import json
import logging

import termplotlib as tpl
from cachetools import FIFOCache
from discord.ext import commands, tasks
from web3 import Web3, WebsocketProvider
from web3.datastructures import MutableAttributeDict as aDict

import utils.embeds
from utils import solidity, readable
from utils.cached_ens import CachedEns
from utils.cfg import cfg
from utils.rocketpool import RocketPool

log = logging.getLogger("events")
log.setLevel(cfg["log_level"])

DEPOSIT_EVENT = 2
WITHDRAWABLE_EVENT = 3


class Events(commands.Cog):
  def __init__(self, bot):
    self.bot = bot
    self.loaded = True
    self.tnx_hash_cache = FIFOCache(maxsize=256)
    self.events = []
    self.internal_event_mapping = {}
    self.topic_mapping = {}

    self.w3 = Web3(WebsocketProvider(f"wss://{cfg['rocketpool.chain']}.infura.io/ws/v3/{cfg['rocketpool.infura_secret']}"))
    self.ens = CachedEns()
    self.rp = RocketPool(self.w3)

    with open("./plugins/events/events.json") as f:
      mapped_events = json.load(f)

    # Load Contracts and create Filters for all Events
    addresses = []
    aggregated_topics = []
    for contract_name, event_mapping in mapped_events.items():
      contract = self.rp.get_contract_by_name(contract_name)
      addresses.append(contract.address)
      self.internal_event_mapping[contract_name] = event_mapping
      for event in event_mapping:
        topic = contract.events[event].build_filter().topics[0]
        self.topic_mapping[topic] = event
        if topic not in aggregated_topics:
          aggregated_topics.append(topic)

    self.events.append(self.w3.eth.filter({
      "address": addresses,
      "topics": [aggregated_topics],
      "fromBlock": "latest",
      "toBlock": "latest"
    }))

    # Track MinipoolStatus.Staking and MinipoolStatus.Withdrawable Events.
    minipool_delegate_contract = self.rp.get_contract(name="rocketMinipoolDelegate")
    self.events.append(minipool_delegate_contract.events.StatusUpdated.createFilter(fromBlock="latest",
                                                                                    toBlock="latest",
                                                                                    argument_filters={
                                                                                      'status': [DEPOSIT_EVENT,
                                                                                                 WITHDRAWABLE_EVENT]}))

    if not self.run_loop.is_running():
      self.run_loop.start()

  def handle_minipool_events(self, event):
    receipt = self.w3.eth.get_transaction_receipt(event.transactionHash)

    if not self.rp.call("rocketMinipoolManager.getMinipoolExists", receipt.to):
      # some random contract we don't care about
      log.warning(f"Skipping {event.transactionHash.hex()} because the called Contract is not a Minipool")
      return None, None

    # first need to make the container mutable
    event = aDict(event)
    # so we can make this mutable
    event.args = aDict(event.args)

    pubkey = self.rp.get_pubkey_using_transaction(receipt)
    if not pubkey:
      # check if the contract has it stored instead
      pubkey = self.rp.call("rocketMinipoolManager.getMinipoolPubkey", receipt["from"]).hex()

    if pubkey:
      event.args.pubkey = pubkey

    # while we are at it add the sender address so it shows up
    event.args["from"] = receipt["from"]
    # and add the minipool address, which is the contract that was called
    event.args.minipool = receipt.to

    event_name = "minipool_deposit_event" if event.args.status == DEPOSIT_EVENT else "minipool_exited_event"
    return self.create_embed(event_name, event), event_name

  def create_embed(self, event_name, event):
    # prepare args
    args = aDict(event['args'])

    # store event_name in args
    args.event_name = event_name

    # add transaction hash and block number to args
    args.transactionHash = event.transactionHash.hex()
    args.blockNumber = event.blockNumber

    # add proposal message manually if the event contains a proposal
    if "proposal" in event_name:
      proposal_id = event.args.proposalID
      args.message = self.rp.call("rocketDAOProposal.getMessage", proposal_id)
      # create bar graph for votes
      votes = [
        solidity.to_int(self.rp.call("rocketDAOProposal.votesFor", proposal_id)),
        solidity.to_int(self.rp.call("rocketDAOProposal.votesAgainst", proposal_id))
      ]
      vote_graph = tpl.figure()
      vote_graph.barh(votes, ["For", "Against"], max_width=20)
      args.vote_graph = vote_graph.get_string()

    # create human readable decision for votes
    if "supported" in args:
      args.decision = "for" if args.supported else "against"

    # add inflation and new supply if inflation occurred
    if "rpl_inflation" in event_name:
      args.total_supply = int(solidity.to_float(self.rp.call("rocketTokenRPL.totalSupply")))
      args.inflation = round(self.rp.get_annual_rpl_inflation() * 100, 4)

    # handle numbers and hex strings
    for arg_key, arg_value in list(args.items()):
      if any(keyword in arg_key.lower() for keyword in ["amount", "value"]):
        args[arg_key] = arg_value / 10 ** 18

      if str(arg_value).startswith("0x"):
        name = ""
        if self.w3.isAddress(arg_value):
          name = self.rp.call("rocketDAONodeTrusted.getMemberID", arg_value)
          if not name:
            # not an odao member, try to get their ens
            name = self.ens.get_name(arg_value)
        if not name:
          # fallback when no ens name/odao id is found or when the hex isn't an address to begin with
          name = readable.hex(arg_value)

        if arg_key == "pubkey":
          args[f"{arg_key}_fancy"] = f"[{name}](https://beaconcha.in/validator/{arg_value})"
        else:
          args[f"{arg_key}_fancy"] = f"[{name}](https://etherscan.io/search?q={arg_value})"

    # add oDAO member name if we can
    if "odao" in event_name:
      keys = [key for key in ["nodeAddress", "canceller", "executer", "proposer", "voter"] if key in args]
      if keys:
        key = keys[0]
        name = self.rp.call("rocketDAONodeTrusted.getMemberID", args[key])
        if name:
          args.member_fancy = f"[{name}](https://etherscan.io/search?q={args[key]})"
        else:
          # fallback to just using the pre-formatted address instead
          args.member_fancy = args[key + '_fancy']

    embed = utils.embeds.assemble(args)

    return embed

  @tasks.loop(seconds=15.0)
  async def run_loop(self):
    if self.loaded:
      try:
        return await self.check_for_new_events()
      except Exception as err:
        self.loaded = False
        log.exception(err)
    try:
      return self.__init__(self.bot)
    except Exception as err:
      self.loaded = False
      log.exception(err)

  async def check_for_new_events(self):
    if not self.loaded:
      return
    log.info("Checking for new Events")

    messages = []
    tnx_hashes = []

    for events in self.events:
      for event in reversed(list(events.get_new_entries())):
        tnx_hash = event.transactionHash.hex()
        event_name = None
        embed = None

        if event.get("removed", False) or tnx_hash in self.tnx_hash_cache:
          continue

        log.debug(f"Checking Event {event}")

        address = event.address
        contract_name = self.rp.get_name_by_address(address)
        if contract_name:
          # default event path
          contract = self.rp.get_contract_by_address(address)
          contract_event = self.topic_mapping[event.topics[0].hex()]
          event = contract.events[contract_event]().processLog(event)
          event_name = self.internal_event_mapping[contract_name][event.event]

          embed = self.create_embed(event_name, event)
        elif event.get("event", None) == "StatusUpdated":
          if tnx_hash in tnx_hashes:
            log.debug("Skipping Event as we have already seen it. (Double statusUpdated Emit Bug)")
            continue
          # deposit/exit event path
          embed, event_name = self.handle_minipool_events(event)

        if embed:
          # lazy way of making it sort events within a single block correctly
          score = event.blockNumber + (event.logIndex / 1000)
          messages.append(aDict({
            "score": score,
            "embed": embed,
            "event_name": event_name
          }))

        tnx_hashes.append(tnx_hash)

    log.debug("Finished Checking for new Events")

    if messages:
      log.info(f"Sending {len(messages)} Message(s)")

      channels = cfg["discord.channels"]

      for message in sorted(messages, key=lambda a: a["score"], reverse=False):
        log.debug(f"Sending \"{message.event_name}\" Event")
        channel_candidates = [value for key, value in channels.items() if message.event_name.startswith(key)]
        channel = await self.bot.fetch_channel(channel_candidates[0] if channel_candidates else channels['default'])
        await channel.send(embed=message["embed"])

      log.info("Finished sending Message(s)")

    # de-dupe logic:
    for tnx_hash in set(tnx_hashes):
      self.tnx_hash_cache[tnx_hash] = True

  def cog_unload(self):
    self.loaded = False
    self.run_loop.cancel()


def setup(bot):
  bot.add_cog(Events(bot))
