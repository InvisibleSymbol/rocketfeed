import json
import logging

from cachetools import FIFOCache
from discord.ext import commands, tasks
from web3.datastructures import MutableAttributeDict as aDict

from utils.cfg import cfg
from utils.embeds import CustomEmbeds
from utils.reporter import report_error
from utils.rocketpool import rp
from utils.shared_w3 import w3

log = logging.getLogger("bootstrap")
log.setLevel(cfg["log_level"])

DEPOSIT_EVENT = 2
WITHDRAWABLE_EVENT = 3


class Bootstrap(commands.Cog):
  def __init__(self, bot):
    self.bot = bot
    self.state = "OK"
    self.tnx_hash_cache = FIFOCache(maxsize=256)
    self.addresses = []
    self.internal_function_mapping = {}

    self.embed = CustomEmbeds()

    self.block_event = w3.eth.filter("latest")

    with open("./plugins/bootstrap/functions.json") as f:
      mapped_events = json.load(f)

    for contract_name, event_mapping in mapped_events.items():
      self.addresses.append(rp.get_address_by_name(contract_name))
      self.internal_function_mapping[contract_name] = event_mapping

    if not self.run_loop.is_running():
      self.run_loop.start()

  def create_embed(self, event_name, event):
    # prepare args
    args = aDict(event.args)

    # store event_name in args
    args.event_name = event_name

    # add transaction hash and block number to args
    args.transactionHash = event.hash.hex()
    args.blockNumber = event.blockNumber

    if "dao_disable" in event_name and not event.confirmDisableBootstrapMode:
      return None

    if "SettingBool" in args.function_name:
      args.value = bool(args.value)

    if event_name == "bootstrap_pdao_multi":
      description_parts = []
      for i in range(len(args.settingContractNames)):
        # these are the only types rocketDAOProtocolProposals checks, so fine to hard code until further changes
        # SettingType.UINT256
        if args.types[i] == 0:
          value = w3.toInt(args.data[i])
        # SettingType.BOOL
        elif args.types[i] == 1:
          value = bool(args.data[i])
        # SettingType.ADDRESS
        elif args.types[i] == 3:
          value = w3.toChecksumAddress(args.data[i])
        else:
          value = "???"
        description_parts.append(
          f"`{args.settingContractNames[i]}`: `{args.settingsPath[i]}` set to `{value}`!"
        )
      args.description = "\n".join(description_parts)

    if event_name == "bootstrap_odao_network_upgrade":
      if args.type == "addContract":
        args.description = f"Contract `{args.name}` has been added!"
      elif args.type == "upgradeContract":
        args.description = f"Contract `{args.name}` has been upgraded!"
      elif args.type == "addABI":
        args.description = f"ABI_[⁽ʷʰᵃᵗ⁾](https://ethereum.org/en/glossary/#abi)_ for Contract `{args.name}` has been added!"
      elif args.type == "upgradeABI":
        args.description = f"ABI[⁽ʷʰᵃᵗ⁾](https://ethereum.org/en/glossary/#abi) of Contract `{args.name}` has been upgraded!"

    args = self.embed.prepare_args(args)
    return self.embed.assemble(args)

  @tasks.loop(seconds=15.0)
  async def run_loop(self):
    if self.state == "STOPPED":
      return

    if self.state != "ERROR":
      try:
        self.state = "OK"
        return await self.check_for_new_transactions()
      except Exception as err:
        self.state = "ERROR"
        await report_error(err)
    try:
      return self.__init__(self.bot)
    except Exception as err:
      log.exception(err)

  async def check_for_new_transactions(self):
    log.info("Checking for new Bootstrap Commands")

    messages = []
    for block_hash in reversed(list(self.block_event.get_new_entries())):
      log.debug(f"Checking Block Hash: {block_hash.hex()}")
      block = w3.eth.get_block(block_hash, full_transactions=True)
      for tnx in block.transactions:
        if tnx.get("removed", False) or tnx.hash in self.tnx_hash_cache:
          continue
        if tnx.to in self.addresses:
          self.tnx_hash_cache[tnx.hash] = True

          contract_name = rp.get_name_by_address(tnx.to)
          contract = rp.get_contract_by_address(tnx.to)

          decoded = contract.decode_function_input(tnx.input)
          log.debug(decoded)

          function = decoded[0].function_identifier
          event_name = self.internal_function_mapping[contract_name].get(function, None)

          if event_name:
            event = aDict(tnx)
            event.args = {}
            for arg, value in decoded[1].items():
              event.args[arg.lstrip("_")] = value
            event.args["timestamp"] = block.timestamp
            event.args["function_name"] = function

            if "disable" in event_name and not event.args.get("confirmDisableBootstrapMode", False):
              continue

            embed = self.create_embed(event_name, event)

            if embed:
              # lazy way of making it sort events within a single block correctly
              score = event.blockNumber
              # sort within block
              score += event.transactionIndex * 10 ** -3
              # sort within transaction
              if "logIndex" in event:
                score += event.logIndex * 10 ** -3

              messages.append(aDict({
                "score": score,
                "embed": embed,
                "event_name": event_name
              }))

    log.debug("Finished Checking for new Bootstrap Commands")

    if messages:
      log.info(f"Sending {len(messages)} Message(s)")

      channel = await self.bot.fetch_channel(cfg["discord.channels.bootstrap"])

      for message in sorted(messages, key=lambda a: a["score"], reverse=False):
        log.debug(f"Sending \"{message.event_name}\" Event")
        await channel.send(embed=message["embed"])

      log.info("Finished sending Message(s)")

  def cog_unload(self):
    self.state = "STOPPED"
    self.run_loop.cancel()


def setup(bot):
  bot.add_cog(Bootstrap(bot))
