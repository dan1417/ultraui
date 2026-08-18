import time
from modules import RonProxy
from bittensor.utils.balance import Balance
from bittensor.core.extrinsics.mev_shield import submit_encrypted_extrinsic
from substrateinterface.exceptions import SubstrateRequestException
from substrateinterface import SubstrateInterface
import bittensor as bt

def _do_proxy_call(real, proxy_wallet, call, proxy_type, mev_shield: bool = False) -> tuple[bool, str]:
    subtensor = bt.Subtensor(network="finney")
    substrate = subtensor.substrate

    time1 = time.time()
    
    proxy_call = substrate.compose_call(
        call_module='Proxy',
        call_function='proxy',
        call_params={
            'real': real,
            'force_proxy_type': proxy_type,
            'call': call,
        }
    )
    
    if mev_shield:
        receipt = submit_encrypted_extrinsic(
            subtensor=subtensor,
            wallet=proxy_wallet,
            call=proxy_call,
            period=None,
            raise_error=False,
            wait_for_inclusion=True,
            wait_for_finalization=False,
            wait_for_revealed_execution=False,
        )

        is_success = receipt.success
        message = receipt.message

    else:
        extrinsic = substrate.create_signed_extrinsic(
            call=proxy_call,
            keypair=proxy_wallet.coldkey,
            # era={'period': 1},
        )
        try:
            receipt = substrate.submit_extrinsic(
                extrinsic,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )
        except SubstrateRequestException as e:
            return False, "Failed"
        
        is_success = receipt.is_success
        message = receipt.error_message

    time2 = time.time()
    print(f"Time taken to do proxy call: {time2 - time1} seconds")
    return is_success, message


def main():
    subtensor = bt.Subtensor(network="finney")

    proxy_wallet_name = "novelty"
    delegator = "5GH2aUTMRUh1RprCgH4x3tRyCaKeUi5BfmYCfs1NARA8R54n"
    hotkey = "5G13iLJy7gL1N4c1dNg33epmzPBPRfwdzLLLQEMHFCPTuyzt"
    netuid = 39
    amount = Balance.from_tao(1000)

    # call = subtensor.substrate.compose_call(
    #     call_module='SubtensorModule',
    #     call_function='remove_stake',
    #     call_params={
    #         "hotkey": hotkey,
    #         "netuid": netuid,
    #         "amount_unstaked": amount.rao,
    #     },
    # )

    call = subtensor.substrate.compose_call(
        call_module='SubtensorModule',
        call_function='swap_stake',
        call_params={
            "hotkey": hotkey,
            "origin_netuid": 0,
            "destination_netuid": netuid,
            # "limit_price": 4100000,
            "alpha_amount": amount.rao,
        },
    )

    # call = subtensor.substrate.compose_call(
    #     call_module='SubtensorModule',
    #     call_function='add_stake_limit',
    #     call_params={
    #         "hotkey": hotkey,
    #         "netuid": netuid,
    #         "amount_staked": amount.rao,
    #         "limit_price": 4100000,
    #         "allow_partial": False,
    #     },
    # )

    # call = subtensor.substrate.compose_call(
    #     call_module='SubtensorModule',
    #     call_function='add_stake',
    #     call_params={
    #         "hotkey": hotkey,
    #         "netuid": netuid,
    #         "amount_staked": amount.rao,
    #     },
    # )

    proxy_wallet = bt.Wallet(
        name=proxy_wallet_name,
    )
    proxy_wallet.unlock_coldkey()

    is_success, message = _do_proxy_call(real=delegator, proxy_wallet=proxy_wallet, call=call, proxy_type='Staking')
    print(f"Is success: {is_success}")
    print(f"Message: {message}")

if __name__ == "__main__":
    main()