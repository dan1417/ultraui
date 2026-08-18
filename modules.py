import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import bittensor as bt
from bittensor.balance import Balance
from pydantic import BaseModel

RAO_PER_TAO = 1_000_000_000

RPC_ENDPOINTS = {
    "local": "ws://localhost:9944",
    "test": "wss://test.finney.opentensor.ai:443",
    "finney": "wss://entrypoint-finney.opentensor.ai:443",
}


class Stake(BaseModel):
    netuid: int
    price: float
    hotkey: str
    balance_in_tao: float
    balance_in_alpha: float


class WalletInfo(BaseModel):
    stakes: List[Stake]
    free: float
    total: float
    timestamp: str
    block_number: int
    delegator: Optional[str] = None
    owner_free: Optional[float] = None
    assist_free: Optional[float] = None
    owner_address: Optional[str] = None
    assist_address: Optional[str] = None


@dataclass
class Price:
    tao: float
    rao: int


@dataclass
class SubnetPool:
    """v11 stand-in for DynamicInfo pool fields used by stake sizing."""

    netuid: int
    tao_in: Balance
    alpha_in: Balance
    price: Price
    is_dynamic: bool


class CachedData:
    def __init__(
        self,
        wallets: Dict[str, WalletInfo],
        subnets: List[Optional[SubnetPool]],
        timestamp: str,
    ):
        self.wallets = wallets
        self.subnets = subnets
        self.timestamp = timestamp


def _as_alpha(amount: Balance, netuid: int) -> Balance:
    if amount.netuid == netuid:
        return amount
    return Balance.from_rao(amount.rao, netuid)


class RonProxy:
    def __init__(self, proxy_wallet: str, network: str, wallet_name: str, delegator: str):
        if network not in RPC_ENDPOINTS:
            raise ValueError(f"Invalid network: {network}")

        self.network = network
        self.delegator = delegator
        self.wallet_name = wallet_name
        self.proxy_wallet = bt.Wallet(name=proxy_wallet)
        self.subtensor = bt.Subtensor(network)
        self._proxied = True

    def _signer(self):
        return self.proxy_wallet.coldkey

    def reset_subtensor(self):
        self.subtensor = bt.Subtensor(network=self.network)

    def _execute(
        self,
        intent,
        *,
        proxy_type: str = "Staking",
        mev_shield: bool = False,
    ) -> Tuple[bool, str]:
        try:
            kwargs = dict(wait_for_inclusion=True, wait_for_finalization=False)
            if self._proxied:
                if mev_shield:
                    print("MEV shield skipped: cannot wrap a proxied call")
                result = self.subtensor.execute(
                    intent,
                    self._signer(),
                    proxy_for=self.delegator,
                    proxy_type=proxy_type,
                    **kwargs,
                )
            elif mev_shield:
                result = self.subtensor.submit_shielded(intent, self._signer(), **kwargs)
            else:
                result = self.subtensor.execute(intent, self._signer(), **kwargs)
            return bool(result.success), result.message
        except Exception as e:
            return False, str(e)

    def add_stake(
        self,
        netuid: int,
        hotkey: str,
        amount: Balance,
        tolerance: float,
        prompt: bool = True,
        mev_shield: bool = False,
        cached_data: CachedData = None,
        allow_partial: bool = True,
        fail: bool = False,
    ) -> None:
        print("Cached data: ", cached_data)

        balance = Balance.from_tao(cached_data.wallets[self.wallet_name].free)
        print("--------------------------------")
        print("Adding stake...")
        print(f"Current balance: {balance}")

        confirm = input(f"Do you really want to stake {amount}? (y/n)") if prompt else "y"
        if confirm != "y":
            return

        subnet_info = cached_data.subnets[netuid] if cached_data and netuid < len(cached_data.subnets) else None
        if not subnet_info:
            print(f"Subnet with netuid {netuid} does not exist")
            return

        if subnet_info.is_dynamic:
            tolerance = ((subnet_info.tao_in.rao + amount.rao) / subnet_info.tao_in.rao) ** 2 - 1
            if fail:
                tolerance /= 3
            print(f"Tolerance calculated: {tolerance}")
            price_with_tolerance = int(subnet_info.price.rao * (1 + tolerance))
        else:
            price_with_tolerance = Balance.from_tao(1).rao

        time1 = time.time()
        is_success, error_message = self._execute(
            bt.AddStakeLimit(
                hotkey_ss58=hotkey,
                netuid=netuid,
                amount_tao=amount.tao,
                limit_price_rao=price_with_tolerance,
                allow_partial=allow_partial,
            ),
            mev_shield=mev_shield,
        )
        print(f"Time taken to do proxy call: {time.time() - time1} seconds")
        if is_success:
            print(f"{amount} added to {netuid} successfully")
        else:
            print(f"Error: {error_message}")

    def add_stake_without_tolerance(
        self,
        netuid: int,
        hotkey: str,
        amount: Balance,
        prompt: bool = True,
        mev_shield: bool = False,
    ) -> None:
        confirm = (
            input(f"Do you really want to stake {amount} without tolerance? (y/n)")
            if prompt
            else "y"
        )
        if confirm != "y":
            return

        time1 = time.time()
        is_success, error_message = self._execute(
            bt.AddStake(
                hotkey_ss58=hotkey,
                netuid=netuid,
                amount_tao=amount.tao,
                slippage_protection=False,
            ),
            mev_shield=mev_shield,
        )
        print(f"Time taken to do proxy call: {time.time() - time1} seconds")
        if is_success:
            print(f"{amount} added to {netuid} successfully")
        else:
            print(f"Error: {error_message}")

    def remove_stake(
        self,
        netuid: int,
        hotkey: str,
        amount: Balance,
        all: bool = False,
        tolerance: float = 0.05,
        prompt: bool = True,
        mev_shield: bool = False,
        cached_data: CachedData = None,
    ) -> None:
        tolerance = 1

        balance = None
        stakes = cached_data.wallets[self.wallet_name].stakes
        for stake in stakes:
            if stake.netuid == netuid and stake.hotkey == hotkey:
                balance = Balance.from_amount(stake.balance_in_alpha, netuid)
                break

        if balance is None:
            print(f"Error: No stake found for netuid {netuid}")
            return

        print("--------------------------------")
        print("Removing stake...")
        print(f"Tolerance set to: {tolerance}")
        print(f"Current alpha balance: {balance}")

        if all:
            confirm = (
                input("Do you really want to unstake all available balance? (y/n)")
                if prompt
                else "y"
            )
            if confirm != "y":
                return
            amount = balance
        else:
            confirm = input(f"Do you really want to unstake {amount}? (y/n)") if prompt else "y"
            if confirm != "y":
                return
            amount = _as_alpha(amount, netuid)

        if amount.rao > balance.rao:
            print("Error: Amount to unstake is greater than current balance")
            return

        subnet_info = cached_data.subnets[netuid] if cached_data and netuid < len(cached_data.subnets) else None
        if not subnet_info:
            print(f"Subnet with netuid {netuid} does not exist")
            return

        if subnet_info.is_dynamic:
            price_with_tolerance = int(subnet_info.price.rao * (1 - tolerance))
        else:
            price_with_tolerance = 1

        is_success, error_message = self._execute(
            bt.RemoveStakeLimit(
                hotkey_ss58=hotkey,
                netuid=netuid,
                amount_alpha="all" if all else max(amount.rao - 1, 0) / RAO_PER_TAO,
                limit_price_rao=max(price_with_tolerance, 0),
                allow_partial=True,
            ),
            mev_shield=mev_shield,
        )
        if is_success:
            print(f"{amount} removed from {netuid} successfully")
        else:
            print(f"Error: {error_message}")

    def move_stake(
        self,
        origin_hotkey: str,
        destination_hotkey: str,
        origin_netuid: int,
        destination_netuid: int,
        amount: Balance,
        all: bool = False,
        prompt: bool = True,
        mev_shield: bool = False,
        cached_data: CachedData = None,
    ) -> None:
        balance = None
        stakes = cached_data.wallets[self.wallet_name].stakes
        for stake in stakes:
            if stake.netuid == origin_netuid and stake.hotkey == origin_hotkey:
                balance = Balance.from_amount(stake.balance_in_alpha, origin_netuid)
                break
        if balance is None:
            print(f"Error: No stake found for netuid {origin_netuid}")
            return

        print("--------------------------------")
        print("Moving stake...")
        print(f"Current alpha balance on netuid {origin_netuid}: {balance}")

        if all:
            confirm = (
                input("Do you really want to move all available stake? (y/n)") if prompt else "y"
            )
            if confirm != "y":
                return
            amount = balance
        else:
            confirm = input(f"Do you really want to move {amount}? (y/n)") if prompt else "y"
            if confirm != "y":
                return
            amount = _as_alpha(amount, origin_netuid)

        if amount.rao > balance.rao:
            print("Error: Amount to move is greater than current stake")
            return

        is_success, error_message = self._execute(
            bt.MoveStake(
                origin_hotkey_ss58=origin_hotkey,
                dest_hotkey_ss58=destination_hotkey,
                origin_netuid=origin_netuid,
                dest_netuid=destination_netuid,
                amount_alpha=amount.amount,
            ),
            mev_shield=mev_shield,
        )
        if is_success:
            print(f"{amount} moved from {origin_netuid} to {destination_netuid} successfully")
        else:
            print(f"Error: {error_message}")

    def burned_register(self, hotkey: str, netuid: int) -> None:
        balance = self.subtensor.balances.get(self.delegator)
        print(f"Current balance: {balance}")

        confirm = input("Do you really want to register? (y/n)")
        if confirm != "y":
            return

        if self._proxied:
            inner = self.subtensor.compose(
                bt.calls.SubtensorModule.burned_register(netuid=netuid, hotkey=hotkey)
            )
            try:
                result = self.subtensor.submit_call(
                    bt.calls.Proxy.proxy(
                        real=self.delegator,
                        force_proxy_type="Registration",
                        call=inner,
                    ),
                    self._signer(),
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                )
                is_success, error_message = bool(result.success), result.message
            except Exception as e:
                is_success, error_message = False, str(e)
        else:
            is_success, error_message = self._execute(
                bt.BurnedRegister(netuid=netuid, hotkey_ss58=hotkey),
                proxy_type="Registration",
                mev_shield=True,
            )

        if is_success:
            print("Register successfully")
        else:
            print(f"Error: {error_message}")

    def transfer(self, destination: str, amount: Balance) -> None:
        balance = self.subtensor.balances.get(self.delegator)
        print(f"Current balance: {balance}")

        confirm = input(f"Do you really want to transfer {amount}? (y/n)")
        if confirm != "y":
            return

        is_success, error_message = self._execute(
            bt.Transfer(dest_ss58=destination, amount_tao=amount.tao, keep_alive=True),
            proxy_type="Transfer",
        )
        if is_success:
            print("Transfer successfully")
        else:
            print(f"Error: {error_message}")

    def transfer_stake(
        self, netuid: int, hotkey: str, destination: str, amount: Balance, all: bool = False
    ) -> None:
        balance = self.subtensor.staking.get(
            coldkey_ss58=self.delegator,
            hotkey_ss58=hotkey,
            netuid=netuid,
        )
        print(f"Current stake: {balance}")

        if all:
            confirm = input("Do you really want to transfer all available stake? (y/n)")
            if confirm != "y":
                return
            amount = balance
        else:
            confirm = input(f"Do you really want to transfer {amount}? (y/n)")
            if confirm != "y":
                return
            amount = _as_alpha(amount, netuid)

        if amount.rao > balance.rao:
            print("Error: Amount to transfer is greater than current stake")
            return

        is_success, error_message = self._execute(
            bt.TransferStake(
                dest_coldkey_ss58=destination,
                hotkey_ss58=hotkey,
                origin_netuid=netuid,
                dest_netuid=netuid,
                amount_alpha=amount.amount,
            ),
            proxy_type="Transfer",
        )
        if is_success:
            print("Stake transferred successfully")
        else:
            print(f"Error: {error_message}")


class CommonWallet(RonProxy):
    """Single coldkey wallet: signs and submits extrinsics directly (no proxy)."""

    def __init__(self, wallet_name: str, network: str, bt_wallet_name: str):
        wallet = bt.Wallet(name=bt_wallet_name)
        super().__init__(
            proxy_wallet=bt_wallet_name,
            network=network,
            wallet_name=wallet_name,
            delegator=wallet.coldkeypub.ss58_address,
        )
        self._proxied = False
