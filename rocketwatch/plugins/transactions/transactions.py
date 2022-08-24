import json
import logging
import warnings

import web3.exceptions
from discord.ext import commands
from web3.datastructures import MutableAttributeDict as aDict

from utils import solidity
from utils.cfg import cfg
from utils.containers import Response
from utils.embeds import assemble, prepare_args
from utils.rocketpool import rp
from utils.shared_w3 import w3

log = logging.getLogger("transactions")
log.setLevel(cfg["log_level"])

DEPOSIT_EVENT = 2
WITHDRAWABLE_EVENT = 3


class QueuedTransactions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.state = "INIT"
        self.addresses = []
        self.internal_function_mapping = {}

        self.block_event = w3.eth.filter("latest")

        with open("./plugins/transactions/functions.json") as f:
            mapped_events = json.load(f)

        for contract_name, event_mapping in mapped_events.items():
            try:
                address = rp.get_address_by_name(contract_name)
            except Exception as e:
                log.exception(e)
                log.error(f"Could not find address for contract {contract_name}")
                continue
            self.addresses.append(address)
            self.internal_function_mapping[contract_name] = event_mapping

    def create_embed(self, event_name, event):
        # prepare args
        args = aDict(event.args)

        # store event_name in args
        args.event_name = event_name

        # add transaction hash and block number to args
        args.transactionHash = event.hash.hex()
        args.blockNumber = event.blockNumber

        if "dao_disable" in event_name and not args.confirmDisableBootstrapMode:
            return None

        if "deposit" in event_name:
            receipt = w3.eth.get_transaction_receipt(args.transactionHash)
            args.burnedValue = solidity.to_float(event.gasPrice * receipt.gasUsed)
            args.node = receipt["from"]
            if "queue" in event_name:
                event = rp.get_contract_by_name("rocketMinipoolQueue").events.MinipoolDequeued()
                # get the amount of dequeues that happend in this transaction using the event logs
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    processed_logs = event.processReceipt(receipt)
                args.count = len(processed_logs)

        if "SettingBool" in args.function_name:
            args.value = bool(args.value)

        if event_name == "bootstrap_pdao_multi":
            description_parts = []
            for i in range(len(args.settingContractNames)):
                # these are the only types rocketDAOProtocolProposals checks, so fine to hard code until further changes
                # SettingType.UINT256
                if args.types[i] == 0:
                    value = w3.toInt(args.values[i])
                # SettingType.BOOL
                elif args.types[i] == 1:
                    value = bool(args.values[i])
                # SettingType.ADDRESS
                elif args.types[i] == 3:
                    value = w3.toChecksumAddress(args.values[i])
                else:
                    value = "???"
                description_parts.append(
                    f"`{args.settingPaths[i]} set to {value}`"
                )
            args.description = "\n".join(description_parts)

        if event_name == "bootstrap_odao_network_upgrade":
            if args.type == "addContract":
                args.description = f"Contract `{args.name}` has been added!"
            elif args.type == "upgradeContract":
                args.description = f"Contract `{args.name}` has been upgraded!"
            elif args.type == "addABI":
                args.description = f"[ABI](https://ethereum.org/en/glossary/#abi) for Contract `{args.name}` has been added!"
            elif args.type == "upgradeABI":
                args.description = f"[ABI](https://ethereum.org/en/glossary/#abi) of Contract `{args.name}` has been upgraded!"
            else:
                raise Exception(f"Network Upgrade of type {args.type} is not known.")

        args = prepare_args(args)
        return assemble(args)

    def run_loop(self):
        if self.state == "RUNNING":
            log.error("Boostrap plugin was interrupted while running. Re-initializing...")
            self.__init__(self.bot)
        return self.check_for_new_transactions()

    def check_for_new_transactions(self):
        log.info("Checking for new Bootstrap Commands")
        payload = []

        do_full_check = self.state == "INIT"
        self.state = "RUNNING"
        if do_full_check:
            log.info("Doing full check")
            latest_block = w3.eth.getBlock("latest").number
            blocks = list(range(latest_block - cfg["core.look_back_distance"], latest_block))
        else:
            blocks = list(self.block_event.get_new_entries())

        for block_hash in blocks:
            log.debug(f"Checking Block: {block_hash}")
            try:
                block = w3.eth.get_block(block_hash, full_transactions=True)
            except web3.exceptions.BlockNotFound:
                log.error(f"Skipping Block {block_hash} as it can't be found")
                continue
            for tnx in block.transactions:
                if "to" not in tnx:
                    # probably a contract creation transaction
                    log.debug(
                        f"Skipping Transaction {tnx.hash.hex()} as it has no `to` parameter. Possible Contract Creation.")
                    continue
                if tnx.to in self.addresses:
                    contract_name = rp.get_name_by_address(tnx.to)

                    # get receipt and check if the transaction reverted using status attribute
                    receipt = w3.eth.get_transaction_receipt(tnx.hash)
                    if contract_name == "rocketNodeDeposit" and receipt.status:
                        log.info(f"Skipping Successful Node Deposit {tnx.hash.hex()}")
                        continue
                    if contract_name != "rocketNodeDeposit" and not receipt.status:
                        log.info(f"Skipping Reverted Transaction {tnx.hash.hex()}")
                        continue

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
                        if not receipt.status:
                            event.args["reason"] = rp.get_revert_reason(tnx)

                        embed = self.create_embed(event_name, event)

                        if embed:
                            payload.append(Response(
                                topic="transactions",
                                embed=embed,
                                event_name=event_name,
                                unique_id=f"{tnx.hash.hex()}:{event_name}",
                                block_number=event.blockNumber,
                                transaction_index=event.transactionIndex
                            ))

        log.debug("Finished Checking for new Bootstrap Commands")
        self.state = "OK"

        return payload


async def setup(bot):
    await bot.add_cog(QueuedTransactions(bot))
