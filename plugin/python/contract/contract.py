"""
Contract implementation for Canopy blockchain plugin.

This file contains the base contract implementation that handles the 'send' transaction.
Matches Go's contract/contract.go structure.
"""

import random
import struct
from typing import Optional, Dict, Any, Union, Protocol, TYPE_CHECKING

UINT64_MAX = (1 << 64) - 1

if TYPE_CHECKING:
    from .plugin import Plugin, Config

# Import proto types
from .proto import (
    PluginCheckRequest,
    PluginCheckResponse,
    PluginDeliverRequest,
    PluginDeliverResponse,
    PluginGenesisRequest,
    PluginGenesisResponse,
    PluginBeginRequest,
    PluginBeginResponse,
    PluginEndRequest,
    PluginEndResponse,
    MessageSend,
    MessageSubmit,
    MessageVote,
    Submission,
    Vote,
    PluginKeyRead,
    PluginStateReadRequest,
    PluginStateWriteRequest,
    PluginSetOp,
    PluginDeleteOp,
    PluginFSMConfig,
    FeeParams,
    Account,
    Pool,
)
from .proto import account_pb2, event_pb2, plugin_pb2, tx_pb2
from google.protobuf import any_pb2

from .error import (
    PluginError,
    err_invalid_address,
    err_invalid_amount,
    err_insufficient_funds,
    err_tx_fee_below_state_limit,
    err_invalid_message_cast,
    err_unmarshal,
)


# State key prefixes owned by this plugin (declared in CONTRACT_CONFIG below)
# ANIMI Chain plugin-owned prefixes.
# MUST stay outside 1-15: Canopy reserves those for core state and panics at
# handshake if a declared prefix collides.
SUBMISSION_PREFIX = b"\x64"  # 100 - one record per meme
VOTE_PREFIX = b"\x65"        # 101 - one record per (meme, voter) pair

# Validation limits
CONTENT_HASH_LEN = 32   # sha256 of the media file
MAX_URI_LEN = 256
MAX_TITLE_LEN = 100
MEDIA_TYPE_IMAGE = 0
MEDIA_TYPE_VIDEO = 1


# Plugin configuration (matching Go's ContractConfig)
CONTRACT_CONFIG = {
    "name": "python_plugin_contract",
    "id": 1,
    "version": 1,
    "supported_transactions": ["send", "submit", "vote"],
    "transaction_type_urls": [
        "type.googleapis.com/types.MessageSend",
        "type.googleapis.com/types.MessageSubmit",
        "type.googleapis.com/types.MessageVote",
    ],
    "event_type_urls": [],
    "custom_state_prefixes": [SUBMISSION_PREFIX, VOTE_PREFIX],
    # Include google/protobuf/any.proto first as it's a dependency of event.proto and tx.proto
    "file_descriptor_protos": [
        any_pb2.DESCRIPTOR.serialized_pb,
        account_pb2.DESCRIPTOR.serialized_pb,
        event_pb2.DESCRIPTOR.serialized_pb,
        plugin_pb2.DESCRIPTOR.serialized_pb,
        tx_pb2.DESCRIPTOR.serialized_pb,
    ],
}


# State key prefixes (matching Go)
ACCOUNT_PREFIX = b"\x01"
POOL_PREFIX = b"\x02"
PARAMS_PREFIX = b"\x07"



# Key generation functions (from keys.py)

def join_len_prefix(*items: Optional[bytes]) -> bytes:
    """Join byte arrays with length prefixes."""
    result = bytearray()
    for item in items:
        if not item:
            continue
        if len(item) > 255:
            raise ValueError(f"Item too long: {len(item)} bytes (max 255)")
        result.append(len(item))
        result.extend(item)
    return bytes(result)


def format_uint64(value: Union[int, str]) -> bytes:
    """Format uint64 as big-endian bytes."""
    if isinstance(value, str):
        value = int(value)
    if not isinstance(value, int) or value < 0 or value >= (1 << 64):
        raise ValueError(f"Invalid uint64 value: {value}")
    return struct.pack('>Q', value)


def key_for_account(address: bytes) -> bytes:
    """Generate state database key for an account."""
    return join_len_prefix(ACCOUNT_PREFIX, address)


def key_for_fee_params() -> bytes:
    """Generate state database key for fee parameters."""
    return join_len_prefix(PARAMS_PREFIX, b"/f/")


def key_for_fee_pool(chain_id: int) -> bytes:
    """Generate state database key for fee pool."""
    return join_len_prefix(POOL_PREFIX, format_uint64(chain_id))


def key_for_submission(content_hash: bytes) -> bytes:
    """State key for a meme submission, addressed by its content hash."""
    return join_len_prefix(SUBMISSION_PREFIX, content_hash)


def submission_prefix() -> bytes:
    """Range-read prefix for listing every submission."""
    return join_len_prefix(SUBMISSION_PREFIX)


def key_for_vote(content_hash: bytes, voter_address: bytes) -> bytes:
    """State key proving a single voter voted on a single submission.

    The existence of this key is what makes double-voting impossible.
    """
    return join_len_prefix(VOTE_PREFIX, content_hash, voter_address)


# Proto marshal/unmarshal utilities

def marshal(message: Any) -> bytes:
    """Marshal object to protobuf bytes."""
    try:
        if hasattr(message, 'SerializeToString'):
            return message.SerializeToString()
        raise ValueError("Message does not support serialization")
    except Exception as err:
        raise err_unmarshal(err)


def unmarshal(message_type: Any, data: Optional[bytes]) -> Optional[Any]:
    """Unmarshal bytes to protobuf message."""
    if not data:
        return None
    try:
        if hasattr(message_type, 'FromString'):
            return message_type.FromString(data)
        raise ValueError("Message type does not support deserialization")
    except Exception as err:
        raise err_unmarshal(err)


class Contract:
    """
    Contract defines the smart contract that implements the extended logic of the nested chain.
    Matches Go's Contract struct.
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        fsm_config: Optional[PluginFSMConfig] = None,
        plugin: Optional["Plugin"] = None,
        fsm_id: Optional[int] = None,
    ):
        self.config = config
        self.fsm_config = fsm_config
        self.plugin = plugin
        self.fsm_id = fsm_id

    def genesis(self, request: PluginGenesisRequest) -> PluginGenesisResponse:
        """Genesis implements logic to import a json file to create the state at height 0."""
        return PluginGenesisResponse()

    def begin_block(self, request: PluginBeginRequest) -> PluginBeginResponse:
        """BeginBlock is code that is executed at the start of applying the block."""
        return PluginBeginResponse()

    async def check_tx(self, request: PluginCheckRequest) -> PluginCheckResponse:
        """CheckTx is code that is executed to statelessly validate a transaction."""
        try:
            if not self.plugin or not self.config:
                raise PluginError(1, "plugin", "plugin or config not initialized")

            # Validate fee - read fee params from state
            resp = await self.plugin.state_read(
                self,
                PluginStateReadRequest(
                    keys=[PluginKeyRead(query_id=random.randint(0, 2**53), key=key_for_fee_params())]
                ),
            )

            if resp.HasField("error"):
                response = PluginCheckResponse()
                response.error.CopyFrom(resp.error)
                return response

            # Convert bytes into fee parameters
            if not resp.results or not resp.results[0].entries:
                raise PluginError(1, "plugin", "Fee parameters not found")

            fee_params_bytes = resp.results[0].entries[0].value
            min_fees = unmarshal(FeeParams, fee_params_bytes)
            if not min_fees:
                raise PluginError(1, "plugin", "Failed to decode fee parameters")

            # Check for minimum fee
            if request.tx.fee < min_fees.send_fee:
                raise err_tx_fee_below_state_limit()

            # Get the message and handle by type
            type_url = request.tx.msg.type_url
            if type_url.endswith("/types.MessageSend"):
                msg = MessageSend()
                msg.ParseFromString(request.tx.msg.value)
                return self._check_message_send(msg)
            elif type_url.endswith("/types.MessageSubmit"):
                submit_msg = MessageSubmit()
                submit_msg.ParseFromString(request.tx.msg.value)
                return self._check_message_submit(submit_msg)
            elif type_url.endswith("/types.MessageVote"):
                vote_msg = MessageVote()
                vote_msg.ParseFromString(request.tx.msg.value)
                return self._check_message_vote(vote_msg)
            else:
                raise err_invalid_message_cast()

        except PluginError as e:
            response = PluginCheckResponse()
            response.error.code = e.code
            response.error.module = e.module
            response.error.msg = e.msg
            return response
        except Exception as err:
            response = PluginCheckResponse()
            response.error.code = 1
            response.error.module = "plugin"
            response.error.msg = str(err)
            return response

    async def deliver_tx(self, request: PluginDeliverRequest) -> PluginDeliverResponse:
        """DeliverTx is code that is executed to apply a transaction."""
        try:
            # Get the message and handle by type
            type_url = request.tx.msg.type_url
            if type_url.endswith("/types.MessageSend"):
                msg = MessageSend()
                msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_send(msg, request.tx.fee, request.tx.memo)
            elif type_url.endswith("/types.MessageSubmit"):
                submit_msg = MessageSubmit()
                submit_msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_submit(
                    submit_msg, request.tx.fee, request.height
                )
            elif type_url.endswith("/types.MessageVote"):
                vote_msg = MessageVote()
                vote_msg.ParseFromString(request.tx.msg.value)
                return await self._deliver_message_vote(
                    vote_msg, request.tx.fee, request.height
                )
            else:
                raise err_invalid_message_cast()

        except PluginError as e:
            response = PluginDeliverResponse()
            response.error.code = e.code
            response.error.module = e.module
            response.error.msg = e.msg
            return response
        except Exception as err:
            response = PluginDeliverResponse()
            response.error.code = 1
            response.error.module = "plugin"
            response.error.msg = str(err)
            return response

    def end_block(self, request: PluginEndRequest) -> PluginEndResponse:
        """EndBlock is code that is executed at the end of applying a block."""
        return PluginEndResponse()


    # ------------------------------------------------------------------
    # ANIMI Chain: meme submission
    # ------------------------------------------------------------------

    def _check_message_submit(self, msg: MessageSubmit) -> PluginCheckResponse:
        """Statelessly validate a 'submit' message.

        Note this only checks shape. Whether the content_hash is already taken
        is a stateful question and is enforced in _deliver_message_submit.
        """
        if len(msg.creator_address) != 20:
            raise err_invalid_address()

        # The content hash IS the identity of the meme, so it must be a real sha256
        if len(msg.content_hash) != CONTENT_HASH_LEN:
            raise PluginError(
                20, "plugin",
                f"content_hash must be {CONTENT_HASH_LEN} bytes, got {len(msg.content_hash)}",
            )

        uri = msg.media_uri.strip()
        if not uri:
            raise PluginError(21, "plugin", "media_uri must not be empty")
        if len(uri) > MAX_URI_LEN:
            raise PluginError(21, "plugin", f"media_uri exceeds {MAX_URI_LEN} chars")
        if not (uri.startswith("ipfs://") or uri.startswith("https://")):
            raise PluginError(21, "plugin", "media_uri must start with ipfs:// or https://")

        if msg.media_type not in (MEDIA_TYPE_IMAGE, MEDIA_TYPE_VIDEO):
            raise PluginError(22, "plugin", "media_type must be 0 (image) or 1 (video)")

        if not msg.title.strip():
            raise PluginError(23, "plugin", "title must not be empty")
        if len(msg.title) > MAX_TITLE_LEN:
            raise PluginError(23, "plugin", f"title exceeds {MAX_TITLE_LEN} chars")

        response = PluginCheckResponse()
        response.authorized_signers.append(msg.creator_address)
        return response

    async def _deliver_message_submit(
        self, msg: MessageSubmit, fee: int, height: int
    ) -> PluginDeliverResponse:
        """Register a new meme. Rejects a content_hash that already exists."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")

        sub_query_id = random.randint(0, 2**53)
        creator_query_id = random.randint(0, 2**53)
        fee_query_id = random.randint(0, 2**53)

        sub_key = key_for_submission(msg.content_hash)
        creator_key = key_for_account(msg.creator_address)
        fee_pool_key = key_for_fee_pool(self.config.chain_id)

        response = await self.plugin.state_read(
            self,
            PluginStateReadRequest(
                keys=[
                    PluginKeyRead(query_id=sub_query_id, key=sub_key),
                    PluginKeyRead(query_id=creator_query_id, key=creator_key),
                    PluginKeyRead(query_id=fee_query_id, key=fee_pool_key),
                ]
            ),
        )
        if response.HasField("error"):
            result = PluginDeliverResponse()
            result.error.CopyFrom(response.error)
            return result

        sub_bytes = creator_bytes = fee_pool_bytes = None
        for resp in response.results:
            value = resp.entries[0].value if resp.entries else None
            if resp.query_id == sub_query_id:
                sub_bytes = value
            elif resp.query_id == creator_query_id:
                creator_bytes = value
            elif resp.query_id == fee_query_id:
                fee_pool_bytes = value

        # Duplicate guard: the same media can only be registered once
        if sub_bytes:
            raise PluginError(24, "plugin", "submission already exists for this content_hash")

        creator_account = unmarshal(Account, creator_bytes) if creator_bytes else Account()
        fee_pool = unmarshal(Pool, fee_pool_bytes) if fee_pool_bytes else Pool()

        if creator_account.amount < fee:
            raise err_insufficient_funds()
        if fee_pool.amount > UINT64_MAX - fee:
            raise err_invalid_amount()

        creator_account.amount -= fee
        fee_pool.amount += fee

        submission = Submission(
            content_hash=msg.content_hash,
            creator_address=msg.creator_address,
            media_uri=msg.media_uri.strip(),
            media_type=msg.media_type,
            title=msg.title.strip(),
            created_height=height,
            vote_count=0,
            total_tipped=0,
        )

        write_resp = await self.plugin.state_write(
            self,
            PluginStateWriteRequest(
                sets=[
                    PluginSetOp(key=sub_key, value=marshal(submission)),
                    PluginSetOp(key=creator_key, value=marshal(creator_account)),
                    PluginSetOp(key=fee_pool_key, value=marshal(fee_pool)),
                ],
                deletes=[],
            ),
        )

        result = PluginDeliverResponse()
        if write_resp.HasField("error"):
            result.error.CopyFrom(write_resp.error)
        return result

    # ------------------------------------------------------------------
    # ANIMI Chain: voting
    # ------------------------------------------------------------------

    def _check_message_vote(self, msg: MessageVote) -> PluginCheckResponse:
        """Statelessly validate a 'vote' message."""
        if len(msg.voter_address) != 20:
            raise err_invalid_address()
        if len(msg.content_hash) != CONTENT_HASH_LEN:
            raise PluginError(
                20, "plugin",
                f"content_hash must be {CONTENT_HASH_LEN} bytes, got {len(msg.content_hash)}",
            )
        # A vote always carries a tip. A free vote would be free to sybil.
        if msg.amount == 0:
            raise err_invalid_amount()

        response = PluginCheckResponse()
        response.authorized_signers.append(msg.voter_address)
        return response

    async def _deliver_message_vote(
        self, msg: MessageVote, fee: int, height: int
    ) -> PluginDeliverResponse:
        """Record a vote and tip the creator.

        Enforces: the submission exists, the voter has not voted on it before,
        and the voter is not the creator.
        """
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")

        sub_query_id = random.randint(0, 2**53)
        vote_query_id = random.randint(0, 2**53)
        voter_query_id = random.randint(0, 2**53)
        fee_query_id = random.randint(0, 2**53)

        sub_key = key_for_submission(msg.content_hash)
        vote_key = key_for_vote(msg.content_hash, msg.voter_address)
        voter_key = key_for_account(msg.voter_address)
        fee_pool_key = key_for_fee_pool(self.config.chain_id)

        response = await self.plugin.state_read(
            self,
            PluginStateReadRequest(
                keys=[
                    PluginKeyRead(query_id=sub_query_id, key=sub_key),
                    PluginKeyRead(query_id=vote_query_id, key=vote_key),
                    PluginKeyRead(query_id=voter_query_id, key=voter_key),
                    PluginKeyRead(query_id=fee_query_id, key=fee_pool_key),
                ]
            ),
        )
        if response.HasField("error"):
            result = PluginDeliverResponse()
            result.error.CopyFrom(response.error)
            return result

        sub_bytes = vote_bytes = voter_bytes = fee_pool_bytes = None
        for resp in response.results:
            value = resp.entries[0].value if resp.entries else None
            if resp.query_id == sub_query_id:
                sub_bytes = value
            elif resp.query_id == vote_query_id:
                vote_bytes = value
            elif resp.query_id == voter_query_id:
                voter_bytes = value
            elif resp.query_id == fee_query_id:
                fee_pool_bytes = value

        if not sub_bytes:
            raise PluginError(25, "plugin", "no submission found for this content_hash")
        if vote_bytes:
            raise PluginError(26, "plugin", "this address has already voted on this submission")

        submission = unmarshal(Submission, sub_bytes)
        if not submission:
            raise PluginError(3, "plugin", "failed to decode submission record")

        # Self-voting would let a creator inflate their own score for just the fee
        if submission.creator_address == msg.voter_address:
            raise PluginError(27, "plugin", "creator cannot vote on their own submission")

        voter_account = unmarshal(Account, voter_bytes) if voter_bytes else Account()
        fee_pool = unmarshal(Pool, fee_pool_bytes) if fee_pool_bytes else Pool()

        if msg.amount > UINT64_MAX - fee:
            raise err_invalid_amount()
        total_debit = msg.amount + fee
        if voter_account.amount < total_debit:
            raise err_insufficient_funds()

        # The creator account is read separately: it may equal neither voter nor fee pool
        creator_key = key_for_account(submission.creator_address)
        creator_query_id = random.randint(0, 2**53)
        creator_resp = await self.plugin.state_read(
            self,
            PluginStateReadRequest(
                keys=[PluginKeyRead(query_id=creator_query_id, key=creator_key)]
            ),
        )
        if creator_resp.HasField("error"):
            result = PluginDeliverResponse()
            result.error.CopyFrom(creator_resp.error)
            return result
        creator_bytes = (
            creator_resp.results[0].entries[0].value
            if creator_resp.results and creator_resp.results[0].entries
            else None
        )
        creator_account = unmarshal(Account, creator_bytes) if creator_bytes else Account()

        if creator_account.amount > UINT64_MAX - msg.amount:
            raise err_invalid_amount()
        if fee_pool.amount > UINT64_MAX - fee:
            raise err_invalid_amount()
        if submission.vote_count == UINT64_MAX or submission.total_tipped > UINT64_MAX - msg.amount:
            raise err_invalid_amount()

        voter_account.amount -= total_debit
        creator_account.amount += msg.amount
        fee_pool.amount += fee

        submission.vote_count += 1
        submission.total_tipped += msg.amount

        vote_record = Vote(
            content_hash=msg.content_hash,
            voter_address=msg.voter_address,
            amount=msg.amount,
            height=height,
        )

        write_resp = await self.plugin.state_write(
            self,
            PluginStateWriteRequest(
                sets=[
                    PluginSetOp(key=vote_key, value=marshal(vote_record)),
                    PluginSetOp(key=sub_key, value=marshal(submission)),
                    PluginSetOp(key=voter_key, value=marshal(voter_account)),
                    PluginSetOp(key=creator_key, value=marshal(creator_account)),
                    PluginSetOp(key=fee_pool_key, value=marshal(fee_pool)),
                ],
                deletes=[],
            ),
        )

        result = PluginDeliverResponse()
        if write_resp.HasField("error"):
            result.error.CopyFrom(write_resp.error)
        return result

    def _check_message_send(self, msg: MessageSend) -> PluginCheckResponse:
        """CheckMessageSend statelessly validates a 'send' message."""
        # Check sender address (must be exactly 20 bytes)
        if len(msg.from_address) != 20:
            raise err_invalid_address()

        # Check recipient address (must be exactly 20 bytes)
        if len(msg.to_address) != 20:
            raise err_invalid_address()

        # Check amount (must be greater than 0)
        if msg.amount == 0:
            raise err_invalid_amount()

        # Return authorized signers (sender must sign)
        response = PluginCheckResponse()
        response.recipient = msg.to_address
        response.authorized_signers.append(msg.from_address)
        return response

    async def _deliver_message_send(self, msg: MessageSend, fee: int, memo: str) -> PluginDeliverResponse:
        """DeliverMessageSend handles a 'send' message."""
        if not self.plugin or not self.config:
            raise PluginError(1, "plugin", "plugin or config not initialized")

        # Generate query IDs
        from_query_id = random.randint(0, 2**53)
        to_query_id = random.randint(0, 2**53)
        fee_query_id = random.randint(0, 2**53)

        # Calculate keys
        from_key = key_for_account(msg.from_address)
        to_key = key_for_account(msg.to_address)
        fee_pool_key = key_for_fee_pool(self.config.chain_id)

        # Get the from and to accounts
        response = await self.plugin.state_read(
            self,
            PluginStateReadRequest(
                keys=[
                    PluginKeyRead(query_id=fee_query_id, key=fee_pool_key),
                    PluginKeyRead(query_id=from_query_id, key=from_key),
                    PluginKeyRead(query_id=to_query_id, key=to_key),
                ]
            ),
        )

        # Check for internal error
        if response.HasField("error"):
            result = PluginDeliverResponse()
            result.error.CopyFrom(response.error)
            return result

        # Get the from bytes and to bytes
        from_bytes = None
        to_bytes = None
        fee_pool_bytes = None

        for resp in response.results:
            if resp.query_id == from_query_id:
                from_bytes = resp.entries[0].value if resp.entries else None
            elif resp.query_id == to_query_id:
                to_bytes = resp.entries[0].value if resp.entries else None
            elif resp.query_id == fee_query_id:
                fee_pool_bytes = resp.entries[0].value if resp.entries else None

        if msg.amount > UINT64_MAX - fee:
            raise err_invalid_amount()

        # Add fee to amount to deduct
        amount_to_deduct = msg.amount + fee

        # Convert bytes to account structures
        from_account = unmarshal(Account, from_bytes) if from_bytes else Account()
        to_account = unmarshal(Account, to_bytes) if to_bytes else Account()
        fee_pool = unmarshal(Pool, fee_pool_bytes) if fee_pool_bytes else Pool()

        # Check sufficient funds
        if from_account.amount < amount_to_deduct:
            raise err_insufficient_funds()

        # For self-transfer, use same account data
        if from_key == to_key:
            to_account = from_account

        if fee_pool.amount > UINT64_MAX - fee or (
            from_key != to_key and to_account.amount > UINT64_MAX - msg.amount
        ):
            raise err_invalid_amount()

        # Subtract from sender
        from_account.amount -= amount_to_deduct

        # Add the fee to the fee pool
        fee_pool.amount += fee

        # Add to recipient
        to_account.amount += msg.amount

        # Convert accounts to bytes
        from_bytes_new = marshal(from_account)
        to_bytes_new = marshal(to_account)
        fee_pool_bytes_new = marshal(fee_pool)

        # Retain drained accounts only when they carry nonce state or core will advance the nonce after RLP.V2 delivery.
        sets = [
            PluginSetOp(key=fee_pool_key, value=fee_pool_bytes_new),
            PluginSetOp(key=to_key, value=to_bytes_new),
        ]
        deletes = []
        if from_account.amount == 0 and from_account.nonce == 0 and memo != "RLP.V2":
            deletes.append(PluginDeleteOp(key=from_key))
        else:
            sets.append(PluginSetOp(key=from_key, value=from_bytes_new))
        write_resp = await self.plugin.state_write(
            self,
            PluginStateWriteRequest(
                sets=sets,
                deletes=deletes,
            ),
        )

        result = PluginDeliverResponse()
        if write_resp.HasField("error"):
            result.error.CopyFrom(write_resp.error)
        return result
