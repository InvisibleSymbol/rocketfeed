import logging
import aiohttp

from utils.cfg import cfg
from utils.shared_w3 import w3

log = logging.getLogger("etherscan")
log.setLevel(cfg["log_level"])


async def get_recent_account_transactions(address, block_count=44800):
    ETHERSCAN_URL = "https://api.etherscan.io/api"

    highest_block = w3.eth.get_block("latest")["number"]
    page = 1
    lowest_block = highest_block - block_count

    async with aiohttp.ClientSession() as session:
        resp = await session.get(ETHERSCAN_URL, params={"address": address,
                                                        "page": page,
                                                        "apikey": cfg["rocketpool.etherscan_secret"],
                                                        "module": "account",
                                                        "action": "txlist",
                                                        "sort": "desc",
                                                        "startblock": lowest_block,
                                                        "endblock": highest_block})

        if not resp.status == 200:
            log.debug(
                f"Error querying etherscan, unexpected HTTP {str(resp.status)}")
            return

        parsed = await resp.json()
        if "message" not in parsed or not parsed["message"].lower() == "ok":
            error = parsed["message"] if "message" in parsed else ""
            r = parsed["result"] if "result" in parsed else ""
            log.debug(f"Error querying {resp.url} - {error} - {r}")
            return

        return {result["hash"]: result for result in parsed["result"] if result["to"] == address.lower()}
