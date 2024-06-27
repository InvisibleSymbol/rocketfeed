import logging
import requests
import numpy as np
import matplotlib.pyplot as plt

from io import BytesIO
from discord import File
from discord.ext import commands
from discord.ext.commands import Context
from discord.ext.commands import hybrid_command

from typing import Optional
from dataclasses import dataclass

from utils import solidity
from utils.cfg import cfg
from utils.embeds import Embed, resolve_ens
from utils.reporter import report_error
from utils.rocketpool import rp
from utils.get_nearest_block import get_block_by_timestamp

log = logging.getLogger("effective_rpl")
log.setLevel(cfg["log_level"])


class PatchesAPI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @dataclass
    class RewardEstimate:
        address: str
        interval: int
        start_time: int
        data_time: int
        end_time: int
        rpl_rewards: float
        eth_rewards: float

        def projection_factor(self) -> float:
            registration_time = rp.call("rocketNodeManager.getNodeRegistrationTime", self.address)
            reward_start_time = max(registration_time, self.start_time)
            return (self.end_time - reward_start_time) / (self.data_time - reward_start_time)

    @staticmethod
    async def get_estimated_rewards(ctx: Context, address: str) -> Optional[RewardEstimate]:
        try:
            patches_res = requests.get(f"https://sprocketpool.net/api/node/{address}").json()
        except Exception as e:
            await report_error(ctx, e)
            await ctx.send("Error fetching node data from SprocketPool API. Blame Patches.")
            return None

        rpl_rewards: Optional[int] = patches_res[address].get('collateralRpl')
        eth_rewards: Optional[int] = patches_res[address].get('smoothingPoolEth')

        if (rpl_rewards is None) or (eth_rewards is None):
            await ctx.send("No data found for this node.")
            return None

        interval_time = rp.call("rocketDAOProtocolSettingsRewards.getRewardsClaimIntervalTime")

        return PatchesAPI.RewardEstimate(
            address=address,
            interval=patches_res['interval'],
            start_time=patches_res["startTime"],
            data_time=patches_res['time'],
            end_time=patches_res["startTime"] + interval_time,
            rpl_rewards=solidity.to_float(rpl_rewards),
            eth_rewards=solidity.to_float(eth_rewards),
        )

    @staticmethod
    def create_embed(title: str, rewards: RewardEstimate) -> Embed:
        embed = Embed()
        embed.title = title
        embed.description = (
            f"Values based on data from <t:{rewards.data_time}:R> (<t:{rewards.data_time}>).\n"
            f"This is for interval {rewards.interval}, which ends <t:{rewards.end_time}:R> (<t:{rewards.end_time}>)."
        )
        return embed

    @hybrid_command()
    async def upcoming_rewards(self, ctx: Context, node_address: str, extrapolate: bool = True):
        await ctx.defer(ephemeral=True)
        display_name, address = await resolve_ens(ctx, node_address)
        if display_name is None:
            return

        rewards = await self.get_estimated_rewards(ctx, address)
        if rewards is None:
            return

        if extrapolate:
            proj_factor = rewards.projection_factor()
            rewards.rpl_rewards *= proj_factor
            rewards.eth_rewards *= proj_factor

        modifier = "Projected" if extrapolate else "Estimated Ongoing"
        title = f"{modifier} Rewards for {display_name}"
        embed = self.create_embed(title, rewards)
        embed.add_field(name="RPL Staking:", value=f"{rewards.rpl_rewards:,.3f} RPL")
        embed.add_field(name="Smoothing Pool:", value=f"{rewards.eth_rewards:,.3f} ETH")
        await ctx.send(embed=embed)

    @hybrid_command()
    async def simulate_rewards(self, ctx: Context, node_address: str, rpl_stake: int):
        await ctx.defer(ephemeral=True)
        display_name, address = await resolve_ens(ctx, node_address)
        if display_name is None:
            return

        rewards = await self.get_estimated_rewards(ctx, address)
        if rewards is None:
            return

        if rewards.rpl_rewards == 0:
            await ctx.send(
                "This node is projected to not earn any RPL rewards, likely due to being undercollateralized. "
                "Not enough data to simulate rewards."
            )
            return

        data_block, _ = get_block_by_timestamp(rewards.data_time)
        rpl_ratio = solidity.to_float(rp.call("rocketNetworkPrices.getRPLPrice", block=data_block))
        borrowed_eth = solidity.to_float(rp.call("rocketNodeStaking.getNodeETHMatched", address, block=data_block))
        actual_rpl_stake = solidity.to_float(rp.call("rocketNodeStaking.getNodeRPLStake", address, block=data_block))

        def rpip_30_weight(staked_rpl: float) -> float:
            rpl_value = staked_rpl * rpl_ratio
            collateral_ratio = rpl_value / borrowed_eth
            if collateral_ratio < 0.1:
                return 0.0
            elif collateral_ratio <= 0.15:
                return 100 * rpl_value
            else:
                return (13.6137 + 2 * np.log(100 * collateral_ratio - 13)) * borrowed_eth

        proj_factor = rewards.projection_factor()
        rewards.rpl_rewards *= proj_factor
        rewards.eth_rewards *= proj_factor

        projected_rewards = rewards.rpl_rewards
        base_weight = rpip_30_weight(actual_rpl_stake)

        def simulate(_stake):
            return projected_rewards * rpip_30_weight(_stake) / base_weight

        simulated_rewards = simulate(rpl_stake)
        rewards.rpl_rewards = simulated_rewards

        fig, ax = plt.subplots(figsize=(5, 2.5))
        ax.grid()

        x_min = min(rpl_stake / 2, actual_rpl_stake / 2)
        x_max = max(rpl_stake * 2, actual_rpl_stake * 5)
        ax.set_xlim((x_min, x_max))

        x = np.arange(x_min, x_max, 10, dtype=int)
        y = np.array(list(map(simulate, x)))
        ax.plot(x, y, color="#eb8e55")

        ax.plot(actual_rpl_stake, projected_rewards, 'o', color='black', label='current')
        ax.annotate(
            f"{projected_rewards:.2f}",
            (actual_rpl_stake, projected_rewards),
            textcoords="offset points",
            xytext=(5, -10),
            ha='left'
        )

        ax.plot(rpl_stake, simulated_rewards, 'o', color='darkred', label='simulated')
        ax.annotate(
            f"{simulated_rewards:.2f}",
            (rpl_stake, simulated_rewards),
            textcoords="offset points",
            xytext=(5, -10),
            ha='left'
        )

        def formatter(_x, _pos) -> str:
            if _x < 1000:
                return f"{_x:.0f}"
            elif _x < 10_000:
                return f"{(_x / 1000):.1f}k"
            elif _x < 1_000_000:
                return f"{(_x / 1000):.0f}k"
            else:
                return f"{(_x / 1_000_000):.1f}m"

        ax.set_xlabel("rpl stake")
        ax.set_ylabel("rewards")
        ax.xaxis.set_major_formatter(formatter)

        ax.legend(loc='lower right')
        fig.tight_layout()

        img = BytesIO()
        fig.savefig(img, format='png')
        img.seek(0)
        plt.close()

        title = f"Simulated Rewards for {display_name} ({rpl_stake:,} RPL Staked)"
        embed = self.create_embed(title, rewards)
        embed.add_field(name="RPL (Current):", value=f"{projected_rewards:,.3f} RPL")
        embed.add_field(name="RPL (Simulated):", value=f"{rewards.rpl_rewards:,.3f} RPL")
        embed.add_field(name="Smoothing Pool:", value=f"{rewards.eth_rewards:,.3f} ETH")
        embed.set_image(url="attachment://graph.png")

        f = File(img, filename="graph.png")
        await ctx.send(embed=embed, files=[f])
        img.close()


async def setup(bot):
    await bot.add_cog(PatchesAPI(bot))
