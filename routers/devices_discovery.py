"""OTA firmware upload, network discovery scan, and device health history."""

import asyncio
import ipaddress

import httpx
from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from state import _add_log, _state

router = APIRouter(tags=["devices"])


@router.post("/devices/{host:path}/ota-upload")
async def ota_upload(host: str, firmware: UploadFile):
    """Flash .bin firmware to device via ESPHome HTTP OTA."""
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    name = device["name"] if device else host
    try:
        data = await firmware.read()
        _add_log(f"{name}: OTA upload started ({len(data)//1024}KB)", "info")
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"http://{host}/update",
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
            if r.status_code < 300:
                _add_log(f"{name}: ✓ OTA complete — device rebooting", "ok")
                return {"ok": True, "message": "Firmware uploaded, device rebooting"}
            else:
                _add_log(f"{name}: OTA failed — HTTP {r.status_code}", "err")
                return JSONResponse(status_code=500,
                    content={"ok": False, "error": f"Device returned HTTP {r.status_code}"})
    except Exception as e:
        _add_log(f"{name}: OTA error — {e}", "err")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@router.get("/discover")
async def discover_devices(subnet: str | None = None):
    """Scan network for ESPHome devices running Midea climate control."""
    if not subnet:
        for d in _state["devices"]:
            host = d["host"]
            try:
                ipaddress.ip_address(host)
                parts = host.rsplit(".", 1)
                subnet = parts[0] + ".0/24"
                break
            except ValueError:
                pass
    if not subnet:
        return {"ok": False, "error": "Cannot determine subnet — provide ?subnet=192.168.27.0/24"}
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid subnet: {subnet}") from None

    _add_log(f"🔍 Scanning {subnet} for ESPHome devices…", "info")
    found = []
    existing_hosts = {d["host"] for d in _state["devices"]}
    # also resolve existing hostnames to IPs for comparison
    existing_ips = set()
    for d in _state["devices"]:
        host = d["host"]
        try:
            ipaddress.ip_address(host)
            existing_ips.add(host)
        except ValueError:
            # it's a hostname — try to resolve it
            try:
                import socket
                ip = socket.gethostbyname(host)
                existing_ips.add(ip)
            except Exception:
                pass

    async def _probe(ip: str):
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                r = await client.get(f"http://{ip}/")
                if r.status_code != 200:
                    return
                rc = await client.get(f"http://{ip}/climate/air_conditioner")
                if rc.status_code == 200:
                    data = rc.json()
                    fw = None
                    mac_suffix = None
                    hostname = None
                    try:
                        rv = await client.get(f"http://{ip}/text_sensor/air_conditioner_esphome_version")
                        if rv.status_code == 200:
                            fw = rv.json().get("value", "").split(" ")[0]
                    except Exception:
                        pass
                    try:
                        rm = await client.get(f"http://{ip}/text_sensor/air_conditioner_mac_address")
                        if rm.status_code == 200:
                            mac = rm.json().get("value", "")
                            if mac:
                                mac_suffix = mac.replace(":", "")[-6:].lower()
                    except Exception:
                        pass
                    # try reverse DNS for a friendly hostname
                    try:
                        import socket
                        hostname = socket.gethostbyaddr(ip)[0]
                    except Exception:
                        pass
                    # smart default name: prefer resolved hostname, else MAC suffix, else IP
                    if hostname and not hostname.replace(".", "").isdigit():
                        clean_hostname = hostname.split(".")[0].replace("air-conditioner-", "AC ")
                        suggested_name = clean_hostname.replace("-", " ").title()
                    elif mac_suffix:
                        suggested_name = f"AC {mac_suffix}"
                    else:
                        suggested_name = f"AC ({ip})"
                    found.append({
                        "ip": ip,
                        "mode": data.get("mode"),
                        "current_temperature": data.get("current_temperature"),
                        "firmware": fw,
                        "hostname": hostname,
                        "mac_suffix": mac_suffix,
                        "suggested_name": suggested_name,
                        "already_configured": ip in existing_hosts or ip in existing_ips,
                    })
        except Exception:
            pass

    hosts = [str(ip) for ip in network.hosts()]
    batch_size = 32
    for i in range(0, len(hosts), batch_size):
        await asyncio.gather(*[_probe(ip) for ip in hosts[i:i+batch_size]])

    _add_log(f"🔍 Discovery complete — {len(found)} ESPHome device(s) found in {subnet}", "ok")
    return {
        "ok": True,
        "subnet": subnet,
        "scanned": len(hosts),
        "found": found,
        "new": [f for f in found if not f["already_configured"]],
    }


@router.get("/devices/{host:path}/health-history")
async def get_health_history(host: str):
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if not device:
        return {"host": host, "history": []}
    return {"host": host, "name": device.get("name"), "history": device.get("_health_history", [])}
