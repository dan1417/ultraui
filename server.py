from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError
from typing import Optional, Dict, Any, List, Tuple
from modules import RonProxy, CommonWallet, WalletInfo, CachedData, Stake, SubnetPool, Price
import uvicorn
import json
import asyncio
import threading
from datetime import datetime
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import bittensor as bt
from bittensor.balance import Balance

RAO_PER_TAO = 1_000_000_000

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('server.log')
    ]
)
logger = logging.getLogger(__name__)

# Load configuration from config.json
logger.info("Loading configuration from config.json")
try:
    with open("config.json", "r") as f:
        config = json.load(f)
    logger.info(f"Configuration loaded successfully. Network: {config['network']}")
except Exception as e:
    logger.error(f"Failed to load config.json: {e}")
    raise

network = config["network"]
API_KEY = config["api_key"]

# Initialize operators for wallets that sign locally (multisig proxy and common)
wallet_proxies: Dict[str, RonProxy] = {}
logger.info("Initializing local signing wallets (multisig and common)")
for wallet_config in config["wallets"]:
    wallet_name = wallet_config["name"]
    wallet_type = wallet_config.get("type", "multisig")

    if wallet_type == "contract":
        logger.info(f"Skipping contract wallet: {wallet_name}")
        continue

    try:
        if wallet_type == "multisig":
            logger.info(f"Initializing multisig wallet: {wallet_name} (proxy: {wallet_config['proxy_wallet']}, delegator: {wallet_config['delegator']})")
            proxy = RonProxy(
                wallet_name=wallet_name,
                proxy_wallet=wallet_config["proxy_wallet"],
                network=network,
                delegator=wallet_config["delegator"],
            )
        elif wallet_type == "common":
            bt_wallet_name = wallet_config.get("wallet", wallet_name)
            logger.info(f"Initializing common wallet: {wallet_name} (wallet: {bt_wallet_name})")
            proxy = CommonWallet(
                wallet_name=wallet_name,
                network=network,
                bt_wallet_name=bt_wallet_name,
            )
        else:
            logger.info(f"Skipping unknown wallet type: {wallet_name} (type: {wallet_type})")
            continue

        proxy.proxy_wallet.unlock_coldkey()
        wallet_proxies[wallet_name] = proxy
        logger.info(f"Wallet '{wallet_name}' initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize wallet '{wallet_name}': {e}")
        raise

logger.info(f"Connecting to {network} network")
monitoring_subtensor = bt.Subtensor(network=network)
logger.info("Subtensor connection established")

TAOBOT_VALIDATOR = "5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u"

app = FastAPI(title="BT Proxy Choji API", version="1.0.0")

# Mount static files
app.mount("/static", StaticFiles(directory="."), name="static")

# Global variables for caching and WebSocket management
cached_data: CachedData = CachedData(wallets={}, subnets=[], timestamp=datetime.now().isoformat())
websocket_connections: List[WebSocket] = []
data_lock = threading.Lock()

def fetch_all_wallet_data(block_number: Optional[int] = None) -> Tuple[Dict[str, WalletInfo], List[Optional[SubnetPool]], Dict[str, str]]:
    """Fetch wallet data for all configured wallets (multisig, common, and contract types)."""
    if block_number is None:
        block_number = monitoring_subtensor.block

    all_addresses = []
    wallet_specs = []  # (wallet_name, wallet_type, spec)

    for wallet_config in config["wallets"]:
        wallet_name = wallet_config["name"]
        wallet_type = wallet_config.get("type", "multisig")

        if wallet_type == "multisig":
            delegator = wallet_config["delegator"]
            all_addresses.append(delegator)
            wallet_specs.append((wallet_name, wallet_type, {"main_idx": len(all_addresses) - 1}))

        elif wallet_type == "common":
            proxy = wallet_proxies.get(wallet_name)
            address = wallet_config.get("address") or (proxy.delegator if proxy else None)
            if not address:
                logger.warning(f"Skipping common wallet '{wallet_name}': no address available")
                continue
            all_addresses.append(address)
            wallet_specs.append((wallet_name, wallet_type, {"main_idx": len(all_addresses) - 1, "address": address}))

        elif wallet_type == "contract":
            contract_addr = wallet_config["contract_address"]
            owner_addr = wallet_config["owner_address"]
            assist_addr = wallet_config["assist_address"]
            all_addresses.extend([contract_addr, owner_addr, assist_addr])
            base_idx = len(all_addresses) - 3
            wallet_specs.append((wallet_name, wallet_type, {
                "contract_idx": base_idx,
                "owner_idx": base_idx + 1,
                "assist_idx": base_idx + 2,
            }))

    wallet_info_list, subnets = fetch_stake_data_for_addresses(all_addresses, block_number)
    all_wallet_data: Dict[str, WalletInfo] = {}
    wallet_types: Dict[str, str] = {}

    for wallet_name, wallet_type, spec in wallet_specs:
        wallet_types[wallet_name] = wallet_type

        if wallet_type in ("multisig", "common"):
            info = wallet_info_list[spec["main_idx"]]
            wallet_config = next((w for w in config["wallets"] if w["name"] == wallet_name), None)
            if wallet_type == "common":
                delegator = spec.get("address") or (wallet_config.get("address") if wallet_config else None)
            else:
                delegator = wallet_config["delegator"] if wallet_config else None
            all_wallet_data[wallet_name] = WalletInfo(**{**info.model_dump(), "delegator": delegator})

        elif wallet_type == "contract":
            contract_info = wallet_info_list[spec["contract_idx"]]
            owner_info = wallet_info_list[spec["owner_idx"]]
            assist_info = wallet_info_list[spec["assist_idx"]]
            wallet_config = next((w for w in config["wallets"] if w["name"] == wallet_name), None)
            contract_addr = wallet_config["contract_address"] if wallet_config else None
            owner_addr = wallet_config["owner_address"] if wallet_config else None
            assist_addr = wallet_config["assist_address"] if wallet_config else None
            all_wallet_data[wallet_name] = WalletInfo(
                stakes=contract_info.stakes,
                free=contract_info.free,
                total=contract_info.total,
                timestamp=contract_info.timestamp,
                block_number=contract_info.block_number,
                delegator=contract_addr,
                owner_free=owner_info.free,
                assist_free=assist_info.free,
                owner_address=owner_addr,
                assist_address=assist_addr,
            )

    return all_wallet_data, subnets, wallet_types


def fetch_stake_data(address: str) -> Dict[str, Any]:
    """Fetch stake data for a single address"""
    result = fetch_stake_data_for_addresses([address])
    return result[0] if result else {}

def _as_int(value):
    """Unwrap SCALE/newtype values (e.g. NetUid) that decode as tuples/dicts."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "little")
    if isinstance(value, dict):
        if len(value) == 1:
            return _as_int(next(iter(value.values())))
        for key in ("value", "netuid", "__value__", "_0"):
            if key in value:
                return _as_int(value[key])
    elif isinstance(value, (tuple, list)):
        if not value:
            return 0
        if len(value) == 1:
            return _as_int(value[0])
        for item in reversed(value):
            try:
                return _as_int(item)
            except (TypeError, ValueError):
                continue
    raise TypeError(f"Cannot coerce {type(value).__name__} to int: {value!r}")


def _build_subnet_index(dynamic_infos, prices: Dict[int, float]) -> List[Optional[SubnetPool]]:
    by_id: Dict[int, SubnetPool] = {}
    for info in dynamic_infos or []:
        if not info:
            continue
        netuid = _as_int(info["netuid"])
        tao_in = Balance.from_rao(_as_int(info["tao_in"]))
        alpha_in = Balance.from_rao(_as_int(info["alpha_in"]), netuid)
        price_tao = 1.0 if netuid == 0 else float(prices.get(netuid) or 0.0)
        by_id[netuid] = SubnetPool(
            netuid=netuid,
            tao_in=tao_in,
            alpha_in=alpha_in,
            price=Price(tao=price_tao, rao=int(price_tao * RAO_PER_TAO)),
            is_dynamic=netuid > 0,
        )
    max_id = max(by_id) if by_id else 0
    return [by_id.get(i) for i in range(max_id + 1)]


def fetch_stake_data_for_addresses(
    addresses: List[str], block_number: Optional[int] = None
) -> Tuple[List[WalletInfo], List[Optional[SubnetPool]]]:
    """Fetch current stake data from subtensor"""
    try:
        if block_number is None:
            block_number = monitoring_subtensor.block

        raw_positions = monitoring_subtensor.runtime(
            bt.runtime_api.StakeInfoRuntimeApi.get_stake_info_for_coldkeys,
            [addresses],
        )
        prices = monitoring_subtensor.prices.alpha_prices()
        dynamic_infos = monitoring_subtensor.runtime(
            bt.runtime_api.SubnetInfoRuntimeApi.get_all_dynamic_info, []
        )
        balances = monitoring_subtensor.balances.get_many(addresses)
        subnets = _build_subnet_index(dynamic_infos, prices)

        positions_by_ck: Dict[str, list] = {}
        for ck, rows in raw_positions or []:
            positions_by_ck[str(ck)] = rows or []

        result = []
        for address in addresses:
            stake_info = positions_by_ck.get(address) or []
            free = balances[address].tao
            total = free
            stakes = []
            for row in stake_info:
                netuid = _as_int(row["netuid"])
                alpha_amount = _as_int(row["stake"]) / RAO_PER_TAO
                price = 1.0 if netuid == 0 else float(prices.get(netuid) or 0.0)
                value = alpha_amount * price
                stakes.append(
                    Stake(
                        netuid=netuid,
                        price=price,
                        balance_in_tao=value,
                        balance_in_alpha=alpha_amount,
                        hotkey=str(row["hotkey"]),
                    )
                )
                total += value

            result.append(
                WalletInfo(
                    stakes=stakes,
                    free=free,
                    total=total,
                    timestamp=datetime.now().isoformat(),
                    block_number=block_number,
                )
            )
        return result, subnets
    except Exception as e:
        logger.error(f"Error fetching stake data for addresses {addresses}: {e}")
        raise

async def broadcast_to_websockets(data: CachedData, wallet_types: Dict[str, str]):
    """Broadcast data to all connected WebSocket clients"""
    if websocket_connections:
        logger.debug(f"Broadcasting data to {len(websocket_connections)} WebSocket clients")
        disconnected = []
        for websocket in websocket_connections:
            try:
                # Convert Pydantic model to dict for JSON serialization, add type
                wallets_dict = {}
                for k, v in data.wallets.items():
                    w = v.model_dump()
                    w["type"] = wallet_types.get(k, "multisig")
                    wallets_dict[k] = w
                data_dict = {
                    "wallets": wallets_dict,
                    "timestamp": data.timestamp
                }
                await websocket.send_text(json.dumps(data_dict))
            except Exception as e:
                logger.warning(f"Failed to send data to WebSocket client: {e}")
                disconnected.append(websocket)
        
        # Remove disconnected clients
        if disconnected:
            logger.info(f"Removing {len(disconnected)} disconnected WebSocket clients")
        for ws in disconnected:
            if ws in websocket_connections:
                websocket_connections.remove(ws)

def block_monitor():
    """Background thread that monitors subtensor blocks and updates data"""
    global cached_data
    logger.info("Block monitor thread started")
    while True:
        try:
            # Wait for next block
            logger.debug("Waiting for next block...")
            monitoring_subtensor.wait_for_block()
            block_number = monitoring_subtensor.block
            logger.info(f"New block detected: {block_number}")
            
            # Fetch new data for all wallets (multisig, common, and contract)
            all_wallet_data, subnets, wallet_types = fetch_all_wallet_data(block_number)

            new_data = CachedData(wallets=all_wallet_data, subnets=subnets, timestamp=datetime.now().isoformat())

            # Update cache
            with data_lock:
                cached_data = new_data
            logger.debug("Cache updated with new stake data")

            # Broadcast to WebSocket clients
            asyncio.run(broadcast_to_websockets(new_data, wallet_types))
            
        except Exception as e:
            logger.error(f"Error in block monitor: {e}", exc_info=True)
            # Wait a bit before retrying
            import time
            logger.info("Waiting 5 seconds before retrying...")
            time.sleep(5)

# Add CORS middleware
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AddStakeRequest(BaseModel):
    wallet_name: str
    api_key: str
    netuid: int
    amount: float
    tolerance: float
    mev_shield: bool
    allow_partial: bool = True
    fail: bool = False
    without_tolerance: bool = False


class RemoveStakeRequest(BaseModel):
    wallet_name: str
    api_key: str
    netuid: int
    amount: float
    hotkey: str
    all: bool
    mev_shield: bool


class MoveStakeRequest(BaseModel):
    wallet_name: str
    api_key: str
    origin: int
    hotkey: str
    dest: int
    amount: float
    all: bool
    mev_shield: bool


class ChangeHotkeyRequest(BaseModel):
    wallet_name: str
    api_key: str
    netuid: int
    origin_hotkey: str
    dest_hotkey: str
    amount: float
    all: bool


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket_connections.append(websocket)
    logger.info(f"WebSocket client connected. Total connections: {len(websocket_connections)}")
    
    # Send initial cached data if available
    with data_lock:
        if cached_data:
            total_stakes = sum(len(wallet_data.stakes) for wallet_data in cached_data.wallets.values())
            logger.info(f"Sending initial cached data to client: {total_stakes} stakes across {len(cached_data.wallets)} wallets")
            wallet_types = {w["name"]: w.get("type", "multisig") for w in config["wallets"]}
            wallets_dict = {}
            for k, v in cached_data.wallets.items():
                w = v.model_dump()
                w["type"] = wallet_types.get(k, "multisig")
                wallets_dict[k] = w
            await websocket.send_text(json.dumps({
                "wallets": wallets_dict,
                "timestamp": cached_data.timestamp,
            }))
        else:
            logger.warning("No cached data available to send to client")
    
    try:
        while True:
            data = await websocket.receive_text()
            logger.debug(f"Received WebSocket message: {data}")
            try:
                msg = json.loads(data)
                msg_type = msg.get("type")
                request_id = msg.get("request_id")

                if msg_type == "add":
                    req = AddStakeRequest(**{k: v for k, v in msg.items() if k not in ("type", "request_id")})
                    if req.api_key != API_KEY:
                        await websocket.send_text(json.dumps({"type": "add_response", "request_id": request_id, "success": False, "detail": "Invalid API key"}))
                    elif req.wallet_name not in wallet_proxies:
                        await websocket.send_text(json.dumps({"type": "add_response", "request_id": request_id, "success": False, "detail": f"Wallet '{req.wallet_name}' not found"}))
                    else:
                        try:
                            if req.without_tolerance:
                                wallet_proxies[req.wallet_name].add_stake_without_tolerance(
                                    netuid=req.netuid, hotkey=TAOBOT_VALIDATOR, amount=Balance.from_tao(req.amount),
                                    prompt=False, mev_shield=req.mev_shield,
                                )
                                await websocket.send_text(json.dumps({"type": "add_response", "request_id": request_id, "success": True, "message": f"Stake added to netuid {req.netuid}"}))
                            else:
                                wallet_proxies[req.wallet_name].add_stake(
                                    netuid=req.netuid, hotkey=TAOBOT_VALIDATOR, amount=Balance.from_tao(req.amount),
                                    tolerance=req.tolerance, mev_shield=req.mev_shield, prompt=False, cached_data=cached_data,
                                    allow_partial=req.allow_partial, fail=req.fail,
                                )
                                await websocket.send_text(json.dumps({"type": "add_response", "request_id": request_id, "success": True, "message": f"Stake added to netuid {req.netuid}"}))
                        except Exception as e:
                            await websocket.send_text(json.dumps({"type": "add_response", "request_id": request_id, "success": False, "detail": str(e)}))

                elif msg_type == "remove":
                    req = RemoveStakeRequest(**{k: v for k, v in msg.items() if k not in ("type", "request_id")})
                    if req.api_key != API_KEY:
                        await websocket.send_text(json.dumps({"type": "remove_response", "request_id": request_id, "success": False, "detail": "Invalid API key"}))
                    elif req.wallet_name not in wallet_proxies:
                        await websocket.send_text(json.dumps({"type": "remove_response", "request_id": request_id, "success": False, "detail": f"Wallet '{req.wallet_name}' not found"}))
                    else:
                        try:
                            wallet_proxies[req.wallet_name].remove_stake(
                                netuid=req.netuid, hotkey=req.hotkey, amount=Balance.from_tao(req.amount),
                                tolerance=1, all=req.all, mev_shield=req.mev_shield, prompt=False, cached_data=cached_data,
                            )
                            await websocket.send_text(json.dumps({"type": "remove_response", "request_id": request_id, "success": True, "message": f"Stake removed from netuid {req.netuid}"}))
                        except Exception as e:
                            await websocket.send_text(json.dumps({"type": "remove_response", "request_id": request_id, "success": False, "detail": str(e)}))

                elif msg_type == "move":
                    req = MoveStakeRequest(**{k: v for k, v in msg.items() if k not in ("type", "request_id")})
                    if req.api_key != API_KEY:
                        await websocket.send_text(json.dumps({"type": "move_response", "request_id": request_id, "success": False, "detail": "Invalid API key"}))
                    elif req.wallet_name not in wallet_proxies:
                        await websocket.send_text(json.dumps({"type": "move_response", "request_id": request_id, "success": False, "detail": f"Wallet '{req.wallet_name}' not found"}))
                    else:
                        try:
                            wallet_proxies[req.wallet_name].move_stake(
                                origin_hotkey=req.hotkey, destination_hotkey=TAOBOT_VALIDATOR,
                                origin_netuid=req.origin, destination_netuid=req.dest,
                                amount=Balance.from_tao(req.amount), all=req.all, mev_shield=req.mev_shield,
                                prompt=False, cached_data=cached_data,
                            )
                            await websocket.send_text(json.dumps({"type": "move_response", "request_id": request_id, "success": True, "message": f"Stake swapped from netuid {req.origin} to {req.dest}"}))
                        except Exception as e:
                            await websocket.send_text(json.dumps({"type": "move_response", "request_id": request_id, "success": False, "detail": str(e)}))

                elif msg_type == "change":
                    req = ChangeHotkeyRequest(**{k: v for k, v in msg.items() if k not in ("type", "request_id")})
                    if req.api_key != API_KEY:
                        await websocket.send_text(json.dumps({"type": "change_response", "request_id": request_id, "success": False, "detail": "Invalid API key"}))
                    elif req.wallet_name not in wallet_proxies:
                        await websocket.send_text(json.dumps({"type": "change_response", "request_id": request_id, "success": False, "detail": f"Wallet '{req.wallet_name}' not found"}))
                    else:
                        try:
                            wallet_proxies[req.wallet_name].move_stake(
                                origin_hotkey=req.origin_hotkey, destination_hotkey=req.dest_hotkey,
                                origin_netuid=req.netuid, destination_netuid=req.netuid,
                                amount=Balance.from_tao(req.amount), all=req.all, mev_shield=False,
                                prompt=False, cached_data=cached_data,
                            )
                            await websocket.send_text(json.dumps({"type": "change_response", "request_id": request_id, "success": True, "message": "Stake changed hotkey"}))
                        except Exception as e:
                            await websocket.send_text(json.dumps({"type": "change_response", "request_id": request_id, "success": False, "detail": str(e)}))

                else:
                    await websocket.send_text(json.dumps({"type": "pong", "data": data}))
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "pong", "data": data}))
            except ValidationError as e:
                try:
                    rid = msg.get("request_id")
                    t = msg.get("type", "")
                    resp_type = f"{t}_response" if t in ("add", "remove", "move", "change") else "error"
                    await websocket.send_text(json.dumps({"type": resp_type, "request_id": rid, "success": False, "detail": str(e)}))
                except NameError:
                    await websocket.send_text(json.dumps({"type": "pong", "data": data}))
    except WebSocketDisconnect:
        websocket_connections.remove(websocket)
        logger.info(f"WebSocket client disconnected. Total connections: {len(websocket_connections)}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        if websocket in websocket_connections:
            websocket_connections.remove(websocket)



@app.post("/add")
async def add_stake(request: AddStakeRequest):
    global cached_data
    
    logger.info(f"Add stake request received for wallet '{request.wallet_name}', netuid {request.netuid}, amount {request.amount} TAO")
    
    # Verify API key
    if request.api_key != API_KEY:
        logger.warning(f"Invalid API key attempt for wallet '{request.wallet_name}'")
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Get the wallet proxy
    if request.wallet_name not in wallet_proxies:
        logger.error(f"Wallet '{request.wallet_name}' not found")
        raise HTTPException(status_code=400, detail=f"Wallet '{request.wallet_name}' not found")
    
    proxy = wallet_proxies[request.wallet_name]
    
    try:
        if request.without_tolerance:
            logger.info(f"Adding {request.amount} TAO stake to netuid {request.netuid} for wallet '{request.wallet_name}' without tolerance")
            proxy.add_stake_without_tolerance(
                netuid=request.netuid,
                hotkey=TAOBOT_VALIDATOR,
                amount=Balance.from_tao(request.amount),
                prompt=False,
                mev_shield=request.mev_shield,
            )
        else:
            logger.info(f"Adding {request.amount} TAO stake to netuid {request.netuid} for wallet '{request.wallet_name}'")
            proxy.add_stake(
                netuid=request.netuid,
                hotkey=TAOBOT_VALIDATOR,
                amount=Balance.from_tao(request.amount),
                tolerance=request.tolerance,
                mev_shield=request.mev_shield,
                prompt=False,
                cached_data=cached_data,
                allow_partial=request.allow_partial,
                fail=request.fail,
            )
        logger.info(f"Successfully added stake to netuid {request.netuid} for wallet '{request.wallet_name}'")
        return {"success": True, "message": f"Stake added to netuid {request.netuid}"}
    except Exception as e:
        logger.error(f"Failed to add stake for wallet '{request.wallet_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/remove")
async def remove_stake(request: RemoveStakeRequest):
    global cached_data

    action = "all stake" if request.all else f"{request.amount} TAO"
    logger.info(f"Remove stake request received for wallet '{request.wallet_name}', netuid {request.netuid}, amount {action}")
    
    # Verify API key
    if request.api_key != API_KEY:
        logger.warning(f"Invalid API key attempt for wallet '{request.wallet_name}'")
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Get the wallet proxy
    if request.wallet_name not in wallet_proxies:
        logger.error(f"Wallet '{request.wallet_name}' not found")
        raise HTTPException(status_code=400, detail=f"Wallet '{request.wallet_name}' not found")
    
    proxy = wallet_proxies[request.wallet_name]
    
    try:
        logger.info(f"Removing {action} from netuid {request.netuid} for wallet '{request.wallet_name}'")
        proxy.remove_stake(
            netuid=request.netuid,
            hotkey=request.hotkey,
            amount=Balance.from_tao(request.amount),
            tolerance=1,
            all=request.all,
            mev_shield=request.mev_shield,
            prompt=False,
            cached_data=cached_data,
        )
        logger.info(f"Successfully removed stake from netuid {request.netuid} for wallet '{request.wallet_name}'")
        return {"success": True, "message": f"Stake removed from netuid {request.netuid}"}
    except Exception as e:
        logger.error(f"Failed to remove stake for wallet '{request.wallet_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/move")
async def move_stake(request: MoveStakeRequest):
    global cached_data

    action = "all stake" if request.all else f"{request.amount} TAO"
    logger.info(f"Move stake request received for wallet '{request.wallet_name}', from netuid {request.origin} to {request.dest}, amount {action}")
    
    # Verify API key
    if request.api_key != API_KEY:
        logger.warning(f"Invalid API key attempt for wallet '{request.wallet_name}'")
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Get the wallet proxy
    if request.wallet_name not in wallet_proxies:
        logger.error(f"Wallet '{request.wallet_name}' not found")
        raise HTTPException(status_code=400, detail=f"Wallet '{request.wallet_name}' not found")
    
    proxy = wallet_proxies[request.wallet_name]
    
    try:
        logger.info(f"Swapping {action} from netuid {request.origin} to {request.dest} for wallet '{request.wallet_name}'")
        proxy.move_stake(
            origin_hotkey=request.hotkey,
            destination_hotkey=TAOBOT_VALIDATOR,
            origin_netuid=request.origin,
            destination_netuid=request.dest,
            amount=Balance.from_tao(request.amount),
            all=request.all,
            mev_shield=request.mev_shield,
            prompt=False,
            cached_data=cached_data,
        )
        logger.info(f"Successfully swapped stake from netuid {request.origin} to {request.dest} for wallet '{request.wallet_name}'")
        return {"success": True, "message": f"Stake swapped from netuid {request.origin} to {request.dest}"}
    except Exception as e:
        logger.error(f"Failed to move stake for wallet '{request.wallet_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/change")
async def change_hotkey(request: ChangeHotkeyRequest):
    global cached_data

    action = "all stake" if request.all else f"{request.amount} TAO"
    logger.info(f"Change hotkey request received for wallet '{request.wallet_name}', for netuid {request.netuid} from {request.origin_hotkey} to {request.dest_hotkey}, amount {action}")
    
    # Verify API key
    if request.api_key != API_KEY:
        logger.warning(f"Invalid API key attempt for wallet '{request.wallet_name}'")
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Get the wallet proxy
    if request.wallet_name not in wallet_proxies:
        logger.error(f"Wallet '{request.wallet_name}' not found")
        raise HTTPException(status_code=400, detail=f"Wallet '{request.wallet_name}' not found")
    
    proxy = wallet_proxies[request.wallet_name]
    
    try:
        logger.info(f"Moving {action} for netuid {request.netuid} from {request.origin_hotkey} to {request.dest_hotkey} for wallet '{request.wallet_name}'")
        proxy.move_stake(
            origin_hotkey=request.origin_hotkey,
            destination_hotkey=request.dest_hotkey,
            origin_netuid=request.netuid,
            destination_netuid=request.netuid,
            amount=Balance.from_tao(request.amount),
            all=request.all,
            mev_shield=False,
            prompt=False,
            cached_data=cached_data,
        )
        logger.info(f"Successfully moved stake for netuid {request.netuid} from {request.origin_hotkey} to {request.dest_hotkey} for wallet '{request.wallet_name}'")
        return {"success": True, "message": f"Stake changed hotkey"}
    except Exception as e:
        logger.error(f"Failed to move stake for wallet '{request.wallet_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reset/{wallet_name}")
async def reset_subtensor(wallet_name: str, api_key: str):
    logger.info(f"Reset subtensor request received for wallet '{wallet_name}'")
    
    # Verify API key
    if api_key != API_KEY:
        logger.warning(f"Invalid API key attempt for wallet '{wallet_name}'")
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Get the wallet proxy
    if wallet_name not in wallet_proxies:
        logger.error(f"Wallet '{wallet_name}' not found")
        raise HTTPException(status_code=400, detail=f"Wallet '{wallet_name}' not found")
    
    proxy = wallet_proxies[wallet_name]
    
    try:
        logger.info(f"Resetting subtensor for wallet '{wallet_name}'")
        proxy.reset_subtensor()
        logger.info(f"Successfully reset subtensor for wallet '{wallet_name}'")
        return {"success": True, "message": f"Subtensor reset for wallet {wallet_name}"}
    except Exception as e:
        logger.error(f"Failed to reset subtensor for wallet '{wallet_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    """
    Serve the frontend HTML file
    """
    logger.debug("Root endpoint accessed")
    return FileResponse('index.html')

@app.get("/api")
async def api_info():
    """
    API information endpoint
    """
    logger.debug("API info endpoint accessed")
    return {
        "message": "BT Proxy Multi-Wallet API",
        "version": "2.0.0",
        "endpoints": {
            "GET /api": "API information",
            "GET /wallets": "List available wallets",
            "WebSocket /ws": "Real-time stake data updates for all wallets",
            "POST /add": "Add stake to a netuid (requires wallet_name, api_key, netuid, hotkey, amount, tolerance, mev_shield)",
            "POST /remove": "Remove stake from a netuid (requires wallet_name, api_key, netuid, hotkey, amount, tolerance, all, mev_shield)",
            "POST /move": "Move stake between netuids (requires wallet_name, api_key, origin_hotkey, destination_hotkey, origin, dest, amount, all, mev_shield)",
            "POST /reset/{wallet_name}": "Reset subtensor for a wallet (requires api_key query param)",
            "GET /stake/{address}": "Get stake data for any address",
        },
    }

@app.get("/wallets")
async def list_wallets():
    """
    List available wallets
    """
    logger.info("List wallets endpoint accessed")
    wallets = []
    for wallet_config in config["wallets"]:
        w = {"name": wallet_config["name"], "type": wallet_config.get("type", "multisig")}
        if w["type"] == "multisig":
            w["delegator"] = wallet_config["delegator"]
            w["proxy_wallet"] = wallet_config["proxy_wallet"]
        elif w["type"] == "common":
            proxy = wallet_proxies.get(wallet_config["name"])
            w["wallet"] = wallet_config.get("wallet", wallet_config["name"])
            w["address"] = wallet_config.get("address") or (proxy.delegator if proxy else None)
        elif w["type"] == "contract":
            w["contract_address"] = wallet_config["contract_address"]
            w["owner_address"] = wallet_config["owner_address"]
            w["assist_address"] = wallet_config["assist_address"]
        wallets.append(w)
    logger.debug(f"Returning {len(wallets)} wallets")
    return {"wallets": wallets}

@app.get("/stake/{address}")
async def get_stake_data(address: str):
    """
    Get stake data for a specific address
    
    Args:
        address: The coldkey address to fetch stake data for
    
    Returns:
        Dictionary containing stakes, free balance, total, timestamp, and block number
    """
    logger.info(f"Stake data request received for address: {address}")
    try:
        stake_data = fetch_stake_data(address)
        logger.info(f"Successfully fetched stake data for address: {address}")
        return stake_data
    except Exception as e:
        logger.error(f"Failed to fetch stake data for address {address}: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed to fetch stake data: {str(e)}")

@app.on_event("startup")
async def startup_event():
    """Initialize the application on startup"""
    global cached_data
    logger.info("=" * 80)
    logger.info("APPLICATION STARTUP")
    logger.info("=" * 80)
    
    # Fetch initial data for all wallets (multisig, common, and contract)
    logger.info("Initializing stake data for all wallets...")

    all_wallet_data, subnets, _ = fetch_all_wallet_data()
    initial_data = CachedData(wallets=all_wallet_data, subnets=subnets, timestamp=datetime.now().isoformat())
    
    # Update cache
    with data_lock:
        cached_data = initial_data
    
    logger.info(f"Initial data loaded for {len(all_wallet_data)} wallets")
    
    # Start background block monitoring thread
    logger.info("Starting block monitor thread...")
    monitor_thread = threading.Thread(target=block_monitor, daemon=True)
    monitor_thread.start()
    logger.info("Block monitor thread started successfully")
    logger.info("=" * 80)
    logger.info(f"Server is ready on port 8727")
    logger.info("=" * 80)

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("=" * 80)
    logger.info("APPLICATION SHUTDOWN")
    logger.info("=" * 80)
    logger.info(f"Closing {len(websocket_connections)} WebSocket connections")
    for ws in websocket_connections:
        try:
            await ws.close()
        except:
            pass
    logger.info("Server shutdown complete")

if __name__ == "__main__":
    logger.info("Starting Ultrade Multi-Wallet API Server")
    uvicorn.run(app, host="0.0.0.0", port=8727, log_level="info")
