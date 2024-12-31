# The MIT License (MIT)
# © 2024 templar.tech

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
# fmt: off

# Global imports
import os
import time
import yaml
import torch
import asyncio
import numpy as np
import bittensor as bt
from typing import Dict, Optional
from bittensor import Wallet
from pydantic import ValidationError

# Local imports
from .logging import logger
from .schemas import Bucket


class ChainManager:
    """Base class for handling chain interactions."""

    def __init__(
        self,
        config,
        netuid: Optional[int] = None,
        metagraph=None,
        hparams=None,
        fetch_interval: int = 60,  # Fetch interval in seconds
        wallet: Optional["bt.wallet"] = None,
        bucket: Optional[Bucket] = None,
    ):
        """
        Initialize chain commitment handler.

        Args:
            subtensor (bt.Subtensor): Subtensor instance for chain operations
            netuid (int): Network UID for chain operations
            metagraph: Metagraph instance containing network state
            hparams: Hyperparameters namespace containing model configuration
            fetch_interval (int): Interval in seconds between fetching commitments
            wallet (bt.wallet, optional): Wallet to sign commitments
            bucket (Bucket, optional): Bucket configuration to commit
        """
        # self.subtensor = bt.subtensor(config=config)
        # chain argument instead
        self.config = config
        self.netuid = netuid
        self.metagraph = metagraph
        self.hparams = hparams or {}

        # Block and window tracking
        self.current_block = 0
        self.current_window = 0
        self.window_duration = self.hparams.blocks_per_window
        self.window_time = 0
        self.window_seeds = {}

        # Events
        self.block_event = asyncio.Event()
        self.new_window_event = asyncio.Event()

        # Initialize bucket storage
        self.commitments = {}
        self.peers = []
        self.fetch_interval = fetch_interval
        self._fetch_task = None

        # Store wallet and bucket
        self.wallet = wallet
        self.bucket = bucket

        # Try to commit bucket to the chain
        if self.wallet and self.bucket:
            # Commit bucket synchronously
            asyncio.run(self.try_commit(self.wallet, self.bucket))
        else:
            logger.warning("Wallet and bucket not provided; skipping try_commit.")

        # Fetch commitments synchronously to populate self.commitments
        self.fetch_commitments()

        # Start fetching commitments
        self.start_commitment_fetcher()

    def start_commitment_fetcher(self):
        """Starts the background task to fetch commitments periodically."""
        if self._fetch_task is None:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            self._fetch_task = loop.create_task(self._fetch_commitments_periodically())

    async def _fetch_commitments_periodically(self):
        """Background task to periodically fetch commitments."""
        while True:
            try:
                commitments = await self.get_commitments()
                if commitments:
                    self.commitments = commitments
                    self.update_peers_with_buckets()
                    logger.debug(f"Updated commitments: {self.commitments}")
            except Exception as e:
                logger.error(f"Error fetching commitments: {e}")
            await asyncio.sleep(self.fetch_interval)

    def get_bucket(self, uid: int) -> Optional[Bucket]:
        """Helper function to get the bucket for a given UID.

        Args:
            uid (int): The UID to retrieve the bucket for.

        Returns:
            Optional[Bucket]: The bucket corresponding to the UID, or None if not found.
        """
        return self.commitments.get(uid)

    def get_all_buckets(self) -> Dict[int, Optional[Bucket]]:
        """Helper function to get all buckets for all UIDs in the metagraph.

        Returns:
            Dict[int, Optional[Bucket]]: Mapping of UIDs to their bucket configurations
        """
        return {uid: self.get_bucket(uid) for uid in self.metagraph.uids}

    def block_to_window(self, block: int) -> int:
        """Returns the slice window based on a block."""
        return int(block / self.hparams.window_length)

    def window_to_seed(self, window: int) -> str:
        """Returns the slice window based on a block."""
        return str(self.subtensor.get_block_hash(window * self.hparams.window_length))

    def block_listener(self, loop):
        """Listens for new blocks and updates current block/window state.

        Args:
            loop: The event loop to run the listener in

        This method subscribes to block headers from the subtensor network and:
        - Updates self.current_block with the latest block number
        - Updates self.current_window when crossing window boundaries
        - Retries on connection errors until stop_event is set
        """

        def handler(event, _u, _s):
            self.current_block = int(event["header"]["number"])
            if (
                int(self.current_block / self.hparams.blocks_per_window)
                != self.current_window
            ):
                self.current_window = int(
                    self.current_block / self.hparams.blocks_per_window
                )

        while not self.stop_event.is_set():
            try:
                bt.subtensor(config=self.config).substrate.subscribe_block_headers(
                    handler
                )
                break
            except Exception:
                time.sleep(1)

    async def commit(self, wallet: "bt.wallet", bucket: Bucket) -> None:
        """Commits bucket configuration to the chain.

        Args:
            wallet (bt.wallet): Wallet to sign the commitment
            bucket (Bucket): Bucket configuration to commit
        """
        subtensor = bt.subtensor(config=self.config)
        concatenated = (
            bucket.account_id + bucket.access_key_id + bucket.secret_access_key
        )
        subtensor.commit(wallet, self.netuid, concatenated)
        logger.info(
            f"Committed bucket configuration to chain for hotkey {wallet.hotkey.ss58_address}"
        )

    async def try_commit(self, wallet: Wallet, bucket: Bucket) -> None:
        """Attempts to verify existing commitment matches current bucket config and commits if not.

        Args:
            wallet (bt.wallet): Wallet to sign the commitment
            bucket (Bucket): Current bucket configuration to verify/commit
        """
        try:
            # Get existing commitment
            commitment = self.get_commitment(
                self.metagraph.hotkeys.index(wallet.hotkey.ss58_address)
            )

            # Convert Bucket objects to concatenated strings for comparison
            commitment_str = (
                commitment.name
                + commitment.access_key_id
                + commitment.secret_access_key
            )
            current_str = bucket.name + bucket.access_key_id + bucket.secret_access_key

            logger.debug(
                f"Comparing:\nCommitment: {commitment_str}\nCurrent: {current_str}"
            )

            if current_str != commitment_str:
                raise ValueError("Bucket commitment data does not match")

        except Exception as e:
            logger.error(f"Commitment error: {str(e)}")
            await self.commit(wallet, bucket)

    def get_commitment(self, uid: int) -> Bucket:
        """Retrieves and parses committed bucket configuration data for a given
        UID.

        This method fetches commitment data for a specific UID from the
        subtensor network and decodes it into a structured format. The
        retrieved data is split into the following fields:
        - Account ID: A string of fixed length 32 characters.
        - Access key ID: A string of fixed length 32 characters.
        - Secret access key: A string of variable length (up to 64 characters).

        The parsed fields are then mapped to an instance of the `Bucket` class.
        When initializing the Bucket object, the account ID is also used as the
        bucket name.

        The retrieval process involves:
        - Fetching the commitment data for the specified UID using the
          configured `netuid` from the subtensor network.
        - Splitting the concatenated string into individual fields based on
          their expected lengths and order.
        - Mapping the parsed fields to a `Bucket` instance.

        **Note:** The order of fields (bucket name, account ID, access key ID,
        secret access key) in the concatenated string is critical for accurate
        parsing.

        Args:
            uid: The UID of the neuron whose commitment data is being
                retrieved.

        Returns:
            Bucket: An instance of the `Bucket` class containing the parsed
                bucket configuration details.

        Raises:
            ValueError: If the parsed data does not conform to the expected
                format for the `Bucket` class.
            Exception: If an error occurs while retrieving the commitment data
                from the subtensor network.
        """

        subtensor = bt.subtensor(config=self.config)
        try:
            concatenated = subtensor.get_commitment(self.netuid, uid)
            logger.success(f"Commitment fetched: {concatenated}")
        except Exception as e:
            raise Exception(f"Couldn't get commitment from uid {uid} because {e}")
        if len(concatenated) != 128:
            raise ValueError(
                f"Commitment '{concatenated}' is of length {len(concatenated)} but should be of length 128."
            )

        try:
            return Bucket(
                name=concatenated[:32],
                account_id=concatenated[:32],
                access_key_id=concatenated[32:64],
                secret_access_key=concatenated[64:],
            )
        except ValidationError as e:
            raise ValueError(f"Invalid data in commitment: {e}")

    async def get_commitments(self, block: Optional[int] = None) -> Dict[int, Bucket]:
        """Retrieves all bucket commitments from the chain.

        Args:
            block (int, optional): Block number to query at

        Returns:
            Dict[int, Bucket]: Mapping of UIDs to their bucket configurations
        """
        # if self.netuid or not self.metagraph:
        #     raise ValueError(
        #         "Subtensor, netuid and metagraph must be set for chain operations"
        #     )
        subtensor = bt.subtensor(config=self.config)
        substrate = subtensor.substrate
        result = substrate.query_map(
            module="Commitments",
            storage_function="CommitmentOf",
            params=[self.netuid],
            block_hash=None if block is None else substrate.get_block_hash(block),
        )

        hotkey_to_uid = dict(zip(self.metagraph.hotkeys, self.metagraph.uids))
        commitments = {}

        for key, value in result:
            hotkey = key.value
            if hotkey not in hotkey_to_uid:
                continue

            uid = hotkey_to_uid[hotkey]
            commitment_info = value.value.get("info", {})
            fields = commitment_info.get("fields", [])

            if not fields or not isinstance(fields[0], dict):
                continue

            field_value = next(iter(fields[0].values()))
            if field_value.startswith("0x"):
                field_value = field_value[2:]

            try:
                concatenated = bytes.fromhex(field_value).decode("utf-8").strip()
                if len(concatenated) != 128:
                    logger.error(
                        f"Invalid commitment length for UID {uid}: {len(concatenated)}"
                    )
                    continue

                bucket = Bucket(
                    name=concatenated[:32],
                    account_id=concatenated[:32],
                    access_key_id=concatenated[32:64],
                    secret_access_key=concatenated[64:],
                )
                commitments[uid] = bucket
                logger.success(f"Retrieved bucket commitment for UID {uid}")

            except Exception as e:
                logger.error(f"Failed to decode commitment for UID {uid}: {e}")
                continue

        return commitments

    async def get_bucket_for_neuron(self, wallet: "bt.wallet") -> Optional[Bucket]:
        """Get bucket configuration for a specific neuron's wallet

        Args:
            wallet (bt.wallet): The wallet to get bucket for

        Returns:
            Optional[Bucket]: The bucket assigned to this neuron, or None if not found
        """
        try:
            # Get UID by finding hotkey's index in metagraph
            uid = self.metagraph.hotkeys.index(wallet.hotkey.ss58_address)
            return await self.get_bucket(uid)
        except ValueError:
            logger.warning(
                f"Hotkey {wallet.hotkey.ss58_address} not found in metagraph"
            )
            return None

    def fetch_commitments(self):
        """Synchronously fetches commitments and updates self.commitments."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        commitments = loop.run_until_complete(self.get_commitments())
        if commitments:
            self.commitments = commitments
            self.update_peers_with_buckets()
            logger.debug(f"Fetched commitments synchronously: {self.commitments}")
        else:
            logger.warning("No commitments fetched.")

    def get_hotkey(self, uid: int) -> Optional[str]:
        """Returns the hotkey for a given UID."""
        # Handle different data types for uids
        if isinstance(self.metagraph.uids, (np.ndarray, torch.Tensor)):
            uids_list = self.metagraph.uids.tolist()
        else:
            uids_list = self.metagraph.uids

        # Handle different data types for hotkeys
        if isinstance(self.metagraph.hotkeys, (np.ndarray, torch.Tensor)):
            hotkeys_list = self.metagraph.hotkeys.tolist()
        else:
            hotkeys_list = self.metagraph.hotkeys

        if uid in uids_list:
            index = uids_list.index(uid)
            return hotkeys_list[index]
        else:
            return None

    def update_peers_with_buckets(self):
        """Updates the list of peers (UIDs) that have buckets, excluding validators."""
        # Create a mapping from UIDs to their stakes
        uid_to_stake = dict(zip(self.metagraph.uids.tolist(), self.metagraph.S.tolist()))
        
        # Filter peers that have buckets and have stake <= 10000 (miners)
        self.peers = [
            int(uid) for uid in self.commitments.keys()
            if uid_to_stake.get(int(uid), 0) <= 10000
        ]
        logger.info(f"Updated peers with buckets (excluding validators): {self.peers}")


def get_own_bucket() -> Bucket:
    """Parses the credentials from .env.yaml to create a Bucket object."""
    env_file = ".env.yaml"
    if not os.path.isfile(env_file):
        logger.error(f"The {env_file} file was not found.")
        raise FileNotFoundError(f"The {env_file} file was not found.")

    try:
        with open(env_file, "r") as file:
            credentials = yaml.safe_load(file)
    except yaml.YAMLError as e:
        logger.error(f"Error parsing {env_file}: {e}")
        raise e

    try:
        account_id = credentials["account_id"]
        read_access_key_id = credentials["read"]["access_key_id"]
        read_secret_access_key = credentials["read"]["secret_access_key"]

        # Create a Bucket object
        bucket = Bucket(
            name=account_id,
            account_id=account_id,
            access_key_id=read_access_key_id,
            secret_access_key=read_secret_access_key,
        )
        logger.debug(f"Parsed bucket from {env_file}: {bucket}")
        return bucket
    except KeyError as e:
        logger.error(f"Missing key in {env_file}: {e}")
        raise e
