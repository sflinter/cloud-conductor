# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from conductor.gpu_pricing import GpuInfo

log = logging.getLogger(__name__)

BASE_URL = "https://console.vast.ai/api/v0"


class VastaiProvider:
    name = "vastai"

    def __init__(self):
        self._api_key = ""

    def configure(self, api_key: str) -> None:
        self._api_key = api_key

    def _req(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{BASE_URL}{path}"
        params = kwargs.pop("params", {})
        params["api_key"] = self._api_key
        return httpx.request(method, url, params=params, timeout=30, **kwargs)

    def create_instance(
        self,
        gpu_type_id: str,
        image_name: str,
        name: str = "",
        disk_gb: int = 40,
        **provider_kwargs,
    ) -> dict | None:
        min_reliability = provider_kwargs.get("vastai_min_reliability", 0.95)
        bid_price = provider_kwargs.get("vastai_bid_price", 0.0)
        num_gpus = provider_kwargs.get("vastai_num_gpus", 1)
        geolocation = provider_kwargs.get("vastai_geolocation", "")

        # Step 1: Search for matching offers
        query = {
            "gpu_name": {"eq": gpu_type_id},
            "num_gpus": {"gte": num_gpus},
            "reliability2": {"gte": min_reliability},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "disk_space": {"gte": disk_gb},
        }
        if geolocation:
            query["geolocation"] = {"eq": geolocation}

        offer_type = "bid" if bid_price > 0 else "on-demand"
        body = {
            "q": query,
            "order": [["dph_total", "asc"]],
            "type": offer_type,
            "allocated_storage": float(disk_gb),
        }

        try:
            resp = self._req("POST", "/bundles/", json=body)
            resp.raise_for_status()
            offers = resp.json().get("offers", [])
        except Exception as e:
            log.error(f"[{name}] Failed to search vast.ai offers: {e}")
            return None

        if not offers:
            log.error(f"[{name}] No vast.ai offers found for {gpu_type_id}")
            return None

        offer = offers[0]  # cheapest (sorted by dph_total)
        offer_id = offer["id"]
        cost_per_hour = offer.get("dph_total", 0.0)
        log.info(f"[{name}] Selected vast.ai offer {offer_id} "
                 f"({gpu_type_id}, ${cost_per_hour:.3f}/hr)")

        # Step 2: Rent the offer
        rent_body = {
            "client_id": "me",
            "image": image_name,
            "disk": float(disk_gb),
            "label": name or "conductor",
            "runtype": "ssh_direc ssh_proxy",
            "onstart": "apt-get update -qq && apt-get install -y -qq rsync > /dev/null 2>&1",
        }
        if bid_price > 0:
            rent_body["price"] = bid_price

        try:
            resp = self._req("PUT", f"/asks/{offer_id}/", json=rent_body)
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            log.error(f"[{name}] Failed to rent vast.ai offer {offer_id}: {e}")
            return None

        instance_id = result.get("new_contract")
        if not instance_id:
            log.error(f"[{name}] No instance ID returned from vast.ai")
            return None

        return {"id": str(instance_id), "cost_per_hour": cost_per_hour}

    def wait_for_ssh(self, instance_id: str, timeout: int = 300) -> tuple[str, int] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = self._req("GET", "/instances", params={"owner": "me"})
                resp.raise_for_status()
                instances = resp.json().get("instances", [])
            except Exception:
                time.sleep(5)
                continue

            for inst in instances:
                if str(inst.get("id")) != str(instance_id):
                    continue

                status = inst.get("actual_status", "")
                if status == "running":
                    host, port = self._extract_ssh(inst)
                    if host and port:
                        return (host, port)
                elif status in ("exited", "error"):
                    return None
                break

            time.sleep(5)
        return None

    def check_exists(self, instance_id: str) -> bool:
        try:
            resp = self._req("GET", "/instances", params={"owner": "me"})
            resp.raise_for_status()
            for inst in resp.json().get("instances", []):
                if str(inst.get("id")) == str(instance_id):
                    return inst.get("actual_status") not in ("exited", "error", None)
        except Exception:
            pass
        return False

    def terminate(self, instance_id: str) -> bool:
        try:
            resp = self._req("DELETE", f"/instances/{instance_id}/", json={})
            resp.raise_for_status()
            return True
        except Exception as e:
            log.warning(f"Failed to terminate vast.ai instance {instance_id}: {e}")
            return False

    def get_gpu_catalog(self) -> list[GpuInfo]:
        body = {
            "q": {"rentable": {"eq": True}, "rented": {"eq": False}},
            "order": [["dph_total", "asc"]],
            "type": "on-demand",
        }
        try:
            resp = self._req("POST", "/bundles/", json=body)
            resp.raise_for_status()
            offers = resp.json().get("offers", [])
        except Exception as e:
            log.error(f"Failed to fetch vast.ai GPU catalog: {e}")
            return []

        # Aggregate by GPU model — take cheapest price per model
        by_gpu: dict[str, GpuInfo] = {}
        for offer in offers:
            gpu_name = offer.get("gpu_name", "")
            if not gpu_name:
                continue
            price = offer.get("dph_total", 0.0) or 0.0
            vram = offer.get("gpu_ram", 0.0) or 0.0  # in MB

            if gpu_name not in by_gpu or price < by_gpu[gpu_name].community_price:
                by_gpu[gpu_name] = GpuInfo(
                    id=gpu_name,
                    display_name=gpu_name,
                    memory_mb=int(vram),
                    community_price=price,
                    secure_price=price,
                    community_available=True,
                    secure_available=True,
                )

        return sorted(by_gpu.values(), key=lambda g: g.community_price)

    def _extract_ssh(self, inst: dict) -> tuple[str | None, int | None]:
        # Try direct port mapping first
        ports = inst.get("ports", {})
        if ports and "22/tcp" in ports:
            mappings = ports["22/tcp"]
            if mappings:
                public_ip = inst.get("public_ipaddr")
                host_port = mappings[0].get("HostPort")
                if public_ip and host_port:
                    return (public_ip, int(host_port))

        # Fall back to ssh_host/ssh_port fields
        ssh_host = inst.get("ssh_host")
        ssh_port = inst.get("ssh_port")
        if ssh_host and ssh_port:
            return (ssh_host, int(ssh_port))

        return (None, None)
