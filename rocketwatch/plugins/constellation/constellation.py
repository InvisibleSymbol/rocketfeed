import logging
import datetime
import humanize

from discord.ext.commands import Cog, Context, hybrid_command
from motor.motor_asyncio import AsyncIOMotorClient

from utils import solidity
from utils.cfg import cfg
from utils.shared_w3 import w3
from utils.rocketpool import rp
from utils.visibility import is_hidden_weak
from utils.embeds import Embed, el_explorer_url


cog_id = "constellation"
log = logging.getLogger(cog_id)
log.setLevel(cfg["log_level"])


class Constellation(Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = AsyncIOMotorClient(cfg["mongodb_uri"]).get_database("rocketwatch")

    async def _fetch_num_operators(self) -> int:
        current_block = w3.eth.get_block('latest').number
        whitelist_contract = rp.get_contract_by_name("Constellation.Whitelist")

        if db_entry := (await self.db.last_checked_block.find_one({"_id": cog_id})):
            last_checked_block = db_entry["block"]
            num_operators = db_entry["operators"]
        else:
            last_checked_block = 20946650 # contract deployment
            num_operators = 0

        def _fetch_interval(_from: int, _to: int) -> int:
            _operators = 0

            _operators += len(whitelist_contract.events.OperatorAdded().getLogs(fromBlock=_from, toBlock=_to))
            _operators -= len(whitelist_contract.events.OperatorRemoved().getLogs(fromBlock=_from, toBlock=_to))
            for event_log in whitelist_contract.events.OperatorsAdded().get_logs(fromBlock=_from, toBlock=_to):
                _operators += len(event_log.args.operators)
            for event_log in whitelist_contract.events.OperatorsAdded().get_logs(fromBlock=_from, toBlock=_to):
                _operators -= len(event_log.args.operators)

            return _operators

        request_block_limit = 50_000
        b_from = last_checked_block + 1
        b_to = b_from + request_block_limit

        # catch up to current block with chunked requests
        while b_to < current_block:
            num_operators += _fetch_interval(b_from, b_to)
            b_from = b_to + 1
            b_to = b_from + request_block_limit

        num_operators += _fetch_interval(b_from, current_block)
        last_checked_block = current_block

        await self.db.last_checked_block.replace_one(
            {"_id": cog_id},
            {"_id": cog_id, "block": last_checked_block, "operators": num_operators},
            upsert=True
        )

        return num_operators

    @hybrid_command()
    async def constellation(self, ctx: Context):
        await ctx.defer(ephemeral=is_hidden_weak(ctx))

        xreth_address: str = rp.get_address_by_name("Constellation.ETHVault")
        xrpl_address: str = rp.get_address_by_name("Constellation.RPLVault")

        supernode_contract = rp.get_contract_by_name("Constellation.SuperNodeAccount")
        distributor_contract = rp.get_contract_by_name("Constellation.OperatorDistributor")
        info_calls: dict[str, int] = {
            res.function_name: res.results[0] for res in rp.multicall.aggregate([
                supernode_contract.functions.getNumMinipools(),
                supernode_contract.functions.getEthStaked(),
                supernode_contract.functions.getEthMatched(),
                supernode_contract.functions.getRplStaked(),
                supernode_contract.functions.bond(),
                supernode_contract.functions.maxValidators(),
                distributor_contract.functions.getTvlEth(),
                distributor_contract.functions.getTvlRpl(),
                distributor_contract.functions.minimumStakeRatio()
            ]).results
        }

        num_minipools: int = info_calls["getNumMinipools"]
        eth_staked: int = solidity.to_int(info_calls["getEthStaked"])
        eth_matched: int = solidity.to_int(info_calls["getEthMatched"])
        rpl_staked: float = solidity.to_float(info_calls["getRplStaked"])
        eth_bond: int = solidity.to_int(info_calls["bond"])
        max_validators: int = info_calls["maxValidators"]

        # update operator count
        num_operators: int = await self._fetch_num_operators()

        tvl_eth: float = solidity.to_float(info_calls["getTvlEth"])
        tvl_rpl: float = solidity.to_float(info_calls["getTvlRpl"])
        min_rpl_stake_ratio: float = solidity.to_float(info_calls["minimumStakeRatio"])

        rpl_ratio: float = solidity.to_float(rp.call("rocketNetworkPrices.getRPLPrice"))
        rpl_stake_pct: float = 100 * rpl_staked * rpl_ratio / eth_matched

        balance_eth: float = solidity.to_float(w3.eth.getBalance(distributor_contract.address))
        balance_rpl: float = solidity.to_float(rp.call("rocketTokenRPL.balanceOf", distributor_contract.address))

        # number of new minipools that can be created with available liquidity
        max_minipools_eth = balance_eth // eth_bond
        max_eth_matched: float = (rpl_staked + balance_rpl) * rpl_ratio / min_rpl_stake_ratio
        max_minipools_rpl = (max_eth_matched - eth_matched) // (32 - eth_bond)
        max_new_minipools: int = max(0, int(min(max_minipools_eth, max_minipools_rpl)))

        # break-even time for new minipools
        operator_commission: float = 0.07
        solo_apr: float = 0.033
        deployment_gas: int = 2_300_000
        gas_price_wei: int = w3.eth.gas_price
        deployment_cost_wei: int = deployment_gas * max(0, gas_price_wei - 5_000_000_000)
        daily_income_wei: int = round((32 - eth_bond) * 1e18 * solo_apr * operator_commission / 365)
        break_even_time = datetime.timedelta(days=round(deployment_cost_wei / daily_income_wei))

        embed = Embed(title="Gravita Constellation")
        embed.add_field(
            name="Node Address",
            value=el_explorer_url(supernode_contract.address, name=" Supernode"),
            inline=False
        )
        embed.add_field(name="Minipools", value=num_minipools)
        embed.add_field(name="Operators", value=num_operators)
        embed.add_field(name="MP Limit", value=max_validators)
        embed.add_field(name="ETH Stake", value=f"{eth_staked:,}")
        embed.add_field(name="RPL Stake", value=f"{rpl_staked:,.2f}")
        embed.add_field(name="RPL Bond", value=f"{rpl_stake_pct:.2f}%")

        # yes, it's really unnecessary to be this specific
        if max_new_minipools > 0:
            mp_creation_status = f"`{max_new_minipools}` new minipool(s) can be created!"
        elif max_minipools_eth > 0:
            mp_creation_status = "Not enough RPL for new minipools."
        elif max_minipools_rpl > 0:
            mp_creation_status = "Not enough ETH for new minipools."
        else:
            mp_creation_status = "Not enough ETH and RPL for new minipools."

        embed.add_field(
            name="Distributor Balances",
            value=f"`{balance_eth:,.2f}` ETH\n"
                  f"`{balance_rpl:,.2f}` RPL\n"
                  f"{mp_creation_status}",
            inline=False
        )
        embed.add_field(name="Gas Price", value=f"{(gas_price_wei / 1e9):.2f} gwei")
        embed.add_field(name="Break-Even", value=humanize.naturaldelta(break_even_time))
        embed.add_field(
            name="Protocol TVL",
            value=f"{el_explorer_url(xreth_address, name=' xrETH')}: `{tvl_eth:,.2f}` ETH\n"
                  f"{el_explorer_url(xrpl_address, name=' xRPL')}: `{tvl_rpl:,.2f}` RPL",
            inline=False
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Constellation(bot))