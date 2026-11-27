"""Small read-only chain queries the SDK needs (bittensor 11.x)."""

from __future__ import annotations

import asyncio


def registered(network: str, netuid: int, hotkey: str) -> bool:
    import bittensor as bt
    import bittensor.metagraph as mg

    async def _fetch() -> bool:
        async with bt.Client(network) as client:
            meta = await mg.fetch(client, netuid, commitments=False)
        return hotkey in [str(h) for h in meta.hotkeys]

    return asyncio.run(_fetch())
