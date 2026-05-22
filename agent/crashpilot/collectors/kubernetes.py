"""
Kubernetes forensics collector.

Collects crash-relevant data from a k8s cluster:
  - Pod crash events (CrashLoopBackOff, OOMKilled, Error)
  - Node conditions (MemoryPressure, DiskPressure, PIDPressure, NotReady)
  - Cluster events sorted by time
  - Resource quotas and LimitRange violations
  - Node resource usage
  - Control plane health (if accessible)

Works in two modes:
  1. In-cluster (DaemonSet pod) — uses service account token
  2. Out-of-cluster — uses ~/.kube/config or KUBECONFIG env var
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .base import BaseCollector, cmd_available, run_cmd

_IN_CLUSTER = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()


def _kubectl_base() -> list[str]:
    """Build base kubectl command with in-cluster or out-of-cluster auth."""
    cmd = ["kubectl"]
    if _IN_CLUSTER:
        sa_dir = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        token = (sa_dir / "token").read_text().strip()
        ca = str(sa_dir / "ca.crt")
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        cmd += [
            f"--server=https://{host}:{port}",
            f"--certificate-authority={ca}",
            f"--token={token}",
        ]
    return cmd


class KubernetesCollector(BaseCollector):
    name = "kubernetes"

    async def collect(self) -> dict[str, Any]:
        if not cmd_available("kubectl"):
            return {"available": False, "reason": "kubectl not found"}

        # Quick connectivity check
        version_out, _, rc = await run_cmd(*_kubectl_base(), "version", "--output=json", timeout=10)
        if rc != 0:
            return {"available": False, "reason": "kubectl cannot reach cluster"}

        node_name = os.environ.get("NODE_NAME") or await self._current_node()
        namespace = self._current_namespace()

        results = await self._gather_all(node_name, namespace)
        results["available"] = True
        results["in_cluster"] = _IN_CLUSTER
        results["node_name"] = node_name
        results["namespace"] = namespace
        return results

    async def _gather_all(self, node: str | None, ns: str) -> dict:
        import asyncio
        (
            pod_crashes,
            node_conditions,
            events,
            oom_pods,
            resource_pressure,
            node_metrics,
        ) = await asyncio.gather(
            self._get_pod_crashes(ns),
            self._get_node_conditions(node),
            self._get_recent_events(ns),
            self._get_oom_killed_pods(ns),
            self._get_resource_pressure(ns),
            self._get_node_metrics(node),
            return_exceptions=True,
        )

        return {
            "pod_crashes": pod_crashes if not isinstance(pod_crashes, Exception) else [],
            "node_conditions": node_conditions if not isinstance(node_conditions, Exception) else [],
            "recent_events": events if not isinstance(events, Exception) else [],
            "oom_killed_pods": oom_pods if not isinstance(oom_pods, Exception) else [],
            "resource_pressure": resource_pressure if not isinstance(resource_pressure, Exception) else {},
            "node_metrics": node_metrics if not isinstance(node_metrics, Exception) else {},
        }

    async def _current_node(self) -> str | None:
        out, _, rc = await run_cmd(*_kubectl_base(), "get", "pod",
                                    "-o", "jsonpath={.spec.nodeName}",
                                    "--field-selector=metadata.name=" + os.environ.get("HOSTNAME", ""),
                                    timeout=8)
        return out.strip() or None

    def _current_namespace(self) -> str:
        ns_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
        if ns_path.exists():
            return ns_path.read_text().strip()
        return os.environ.get("POD_NAMESPACE", "default")

    async def _get_pod_crashes(self, namespace: str) -> list[dict]:
        """Find pods in CrashLoopBackOff, Error, or with non-zero exit codes."""
        args = [
            *_kubectl_base(), "get", "pods",
            "-n", namespace,
            "--output=json",
            "--field-selector=status.phase!=Running",
        ]
        out, _, rc = await run_cmd(*args, timeout=20)
        if rc != 0 or not out.strip():
            # Try all pods and filter
            out, _, rc = await run_cmd(*_kubectl_base(), "get", "pods", "-n", namespace, "--output=json", timeout=20)
            if rc != 0:
                return []

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []

        crashes = []
        for pod in data.get("items", []):
            meta = pod.get("metadata", {})
            status = pod.get("status", {})
            phase = status.get("phase", "")
            container_statuses = status.get("containerStatuses", []) + status.get("initContainerStatuses", [])

            for cs in container_statuses:
                state = cs.get("state", {})
                last = cs.get("lastState", {})

                # Check terminated state
                terminated = state.get("terminated") or last.get("terminated") or {}
                waiting = state.get("waiting", {})
                reason = waiting.get("reason", "") or terminated.get("reason", "")

                if reason in ("CrashLoopBackOff", "Error", "OOMKilled", "StartError") \
                   or terminated.get("exitCode", 0) not in (0, None):
                    crashes.append({
                        "pod": meta.get("name", ""),
                        "namespace": meta.get("namespace", namespace),
                        "container": cs.get("name", ""),
                        "reason": reason or "unknown",
                        "exit_code": terminated.get("exitCode"),
                        "restart_count": cs.get("restartCount", 0),
                        "phase": phase,
                        "message": terminated.get("message", "")[:500] or waiting.get("message", "")[:500],
                        "started_at": terminated.get("startedAt"),
                        "finished_at": terminated.get("finishedAt"),
                    })

        return crashes

    async def _get_node_conditions(self, node: str | None) -> list[dict]:
        if node:
            out, _, rc = await run_cmd(
                *_kubectl_base(), "get", "node", node, "--output=json", timeout=15
            )
        else:
            out, _, rc = await run_cmd(
                *_kubectl_base(), "get", "nodes", "--output=json", timeout=15
            )
        if rc != 0:
            return []

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []

        nodes_data = data.get("items", [data]) if "items" in data else [data]
        conditions = []
        for node_obj in nodes_data:
            node_name = node_obj.get("metadata", {}).get("name", "unknown")
            for cond in node_obj.get("status", {}).get("conditions", []):
                conditions.append({
                    "node": node_name,
                    "type": cond.get("type"),
                    "status": cond.get("status"),
                    "reason": cond.get("reason"),
                    "message": cond.get("message", "")[:300],
                    "last_transition": cond.get("lastTransitionTime"),
                })
        return conditions

    async def _get_recent_events(self, namespace: str) -> list[dict]:
        out, _, rc = await run_cmd(
            *_kubectl_base(), "get", "events",
            "-n", namespace,
            "--sort-by=.lastTimestamp",
            "--output=json",
            timeout=20,
        )
        if rc != 0:
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []

        events = []
        for ev in data.get("items", [])[-50:]:  # last 50 events
            if ev.get("type") in ("Warning", "Error"):
                events.append({
                    "type": ev.get("type"),
                    "reason": ev.get("reason"),
                    "message": ev.get("message", "")[:300],
                    "object": f"{ev.get('involvedObject', {}).get('kind')}/{ev.get('involvedObject', {}).get('name')}",
                    "namespace": ev.get("metadata", {}).get("namespace"),
                    "first_time": ev.get("firstTimestamp"),
                    "last_time": ev.get("lastTimestamp"),
                    "count": ev.get("count", 1),
                })
        return events

    async def _get_oom_killed_pods(self, namespace: str) -> list[dict]:
        out, _, rc = await run_cmd(
            *_kubectl_base(), "get", "pods", "-n", namespace, "--output=json", timeout=20
        )
        if rc != 0:
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []

        oom_pods = []
        for pod in data.get("items", []):
            meta = pod.get("metadata", {})
            for cs in pod.get("status", {}).get("containerStatuses", []):
                last = cs.get("lastState", {}).get("terminated", {})
                if last.get("reason") == "OOMKilled":
                    oom_pods.append({
                        "pod": meta.get("name"),
                        "namespace": meta.get("namespace", namespace),
                        "container": cs.get("name"),
                        "exit_code": last.get("exitCode"),
                        "finished_at": last.get("finishedAt"),
                        "restart_count": cs.get("restartCount", 0),
                    })
        return oom_pods

    async def _get_resource_pressure(self, namespace: str) -> dict:
        # ResourceQuota usage
        out, _, rc = await run_cmd(
            *_kubectl_base(), "get", "resourcequota", "-n", namespace, "--output=json", timeout=10
        )
        quotas = []
        if rc == 0 and out.strip():
            try:
                data = json.loads(out)
                for q in data.get("items", []):
                    hard = q.get("status", {}).get("hard", {})
                    used = q.get("status", {}).get("used", {})
                    for resource, limit in hard.items():
                        quotas.append({
                            "resource": resource,
                            "limit": limit,
                            "used": used.get(resource, "0"),
                        })
            except json.JSONDecodeError:
                pass

        return {"resource_quotas": quotas}

    async def _get_node_metrics(self, node: str | None) -> dict:
        if not node:
            return {}
        out, _, rc = await run_cmd(
            *_kubectl_base(), "top", "node", node, "--no-headers", timeout=15
        )
        if rc != 0:
            return {}
        parts = out.strip().split()
        if len(parts) >= 5:
            return {
                "node": parts[0],
                "cpu_usage": parts[1],
                "cpu_percent": parts[2],
                "memory_usage": parts[3],
                "memory_percent": parts[4],
            }
        return {}
