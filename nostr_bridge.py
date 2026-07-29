#!/usr/bin/env python3
"""
nostr_bridge.py — Bridge @PeptideFrontier X posts to Nostr as a thread.
Each post becomes a separate Nostr note (kind 1), threaded via e-tags.
Leon's npub is tagged in every note so he can zap.

Usage:
    NOSTR_NSEC=nsecXXX python nostr_bridge.py
"""

import os
import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard-coded thread content scraped from @PeptideFrontier on 2026-07-29
# ---------------------------------------------------------------------------

# Leon's npub (hex pubkey decoded from bech32)
LEON_NPUB = "npub1f48yzh6j8uthn886t09nz7kvzt7fykhjctfvt55k90266j89us4srqxpke"

POSTS = [
    {
        "text": (
            "BPC-157's therapeutic potential is often overstated. "
            "Many claims trace back to preclinical studies, primarily in rodent models of injury. "
            "No large-scale human trials have established efficacy or safety for widespread use.\n"
            "\n"
            "[bridged from @PeptideFrontier on X • https://x.com/PeptideFrontier/status/2082431796327993655]"
        )
    },
    {
        "text": (
            "Viking's oral VK2735 showed a 13.1% weight loss at 12 weeks in a Phase 1 trial (2026). "
            "This dual GLP-1/GIP agonist's oral form expands options, "
            "though injectables like in the VENTURE study (2026) still lead in magnitude for weight management.\n"
            "\n"
            "[bridged from @PeptideFrontier on X • https://x.com/PeptideFrontier/status/2082494646249574864]"
        )
    },
    {
        "text": (
            "FDA advisory committee voted 8-6 to keep BPC-157 on the compounding list for ulcerative colitis, "
            "overriding FDA staff. Half the panel seats were refilled this year with peptide industry ties. "
            "Still needs final FDA sign-off (non-binding).\n"
            "\n"
            "[bridged from @PeptideFrontier on X • https://x.com/PeptideFrontier/status/2081679739144831074]"
        )
    },
    {
        "text": (
            "Breaking: Dr. Predrag Sikiric, creator of BPC-157, still uses it daily and prefers the oral form. "
            "He opens a capsule, mixes the powder into water, and takes it multiple times per day due to its ~6-hour half-life.\n"
            "\n"
            "[bridged from @PeptideFrontier on X • https://x.com/PeptideFrontier/status/2081647966582980646]"
        )
    },
]


def npub_to_hex(npub: str) -> str:
    """Convert bech32 npub to hex pubkey."""
    from basic_nostr.bech32 import bech32_decode, convertbits
    hrp, data = bech32_decode(npub)
    if data is None:
        raise ValueError(f"Invalid bech32: {npub}")
    decoded = convertbits(data, 5, 8, False)
    return bytes(decoded).hex()


def post_thread(nsec: str):
    from basic_nostr import NostrClient

    # Resolve Leon's hex pubkey for p-tag
    try:
        leon_hex = npub_to_hex(LEON_NPUB)
        log.info("Leon hex pubkey: %s", leon_hex)
    except Exception as e:
        log.warning("Could not decode Leon npub: %s. Skipping p-tag.", e)
        leon_hex = None

    parent_event_id = None
    root_event_id = None

    with NostrClient(nsec) as client:
        for i, post in enumerate(POSTS):
            text = post["text"]

            # Append Leon mention so he gets notified
            if leon_hex:
                text += f"\n\nnostr:{LEON_NPUB}"

            # Build tags
            tags = []
            if leon_hex:
                tags.append(["p", leon_hex])

            if root_event_id:
                # Thread: tag root + reply-to-parent
                tags.append(["e", root_event_id, "", "root"])
            if parent_event_id and parent_event_id != root_event_id:
                tags.append(["e", parent_event_id, "", "reply"])

            try:
                # basic_nostr make_post signature: (content, tags=None)
                event_id = client.make_post(text, tags=tags if tags else None)
                log.info("Posted note %d/%d event_id=%s", i + 1, len(POSTS), event_id)

                if root_event_id is None:
                    root_event_id = event_id
                parent_event_id = event_id

            except TypeError:
                # Fallback: older basic_nostr without tags support
                event_id = client.make_post(text)
                log.info("Posted note %d/%d (no-tags fallback) event_id=%s", i + 1, len(POSTS), event_id)

                if root_event_id is None:
                    root_event_id = event_id
                parent_event_id = event_id

            # Brief pause between posts to avoid relay rate-limiting
            time.sleep(2)

    log.info("Thread bridge complete. Root event: %s", root_event_id)


if __name__ == "__main__":
    nsec = os.getenv("NOSTR_NSEC", "").strip()
    if not nsec:
        log.error("NOSTR_NSEC environment variable is not set.")
        sys.exit(1)
    post_thread(nsec)
