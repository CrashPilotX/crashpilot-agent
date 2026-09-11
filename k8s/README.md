# Kubernetes node agent

The CrashPilot DaemonSet deploys one privileged node-forensics agent per Linux
node.

Create a join token on the dashboard's Systems page (one per cluster; mark it
ephemeral for autoscaled node pools), then:

```bash
kubectl create namespace crashpilot
kubectl -n crashpilot create secret generic crashpilot-agent \
  --from-literal=CRASHPILOT_ENROLL_TOKEN='cpjoin_...'
kubectl apply -f k8s/daemonset.yaml
kubectl rollout status daemonset/crashpilot-agent -n crashpilot
```

Create the Secret before the DaemonSet: pods read it only when they start, so
a pod that started first runs without a token until it is restarted
(`kubectl -n crashpilot rollout restart daemonset/crashpilot-agent`).

Each node enrolls itself on its first heartbeat under `k8s:<node name>`, so
every node appears as its own system. Its credentials are kept on the node in
`/var/lib/crashpilot/.env`, so pod restarts and rollouts keep the same system.
When a pod is stopped, the container stops its heartbeat loop and then signs
the node off, and a node that never comes back is retired automatically if the
token is ephemeral. `k8s/secret.example.yaml` shows the same Secret as a
manifest.

Sign-off needs the pod to be stopped gracefully. `kubectl drain` does not evict
DaemonSet pods, so a drained node keeps reporting until it shuts down. When a
node shuts down or is scaled in, pods are only stopped gracefully if kubelet's
[graceful node shutdown](https://kubernetes.io/docs/concepts/cluster-administration/node-shutdown/)
is configured (`shutdownGracePeriod`); without it, the node reads as an outage
rather than a clean shutdown.

The image tag is `:latest`, so the DaemonSet pulls it on every pod start
(`imagePullPolicy: Always`). Pin a version tag to control upgrades yourself.

The deployment is tested in Kind on every relevant change. It requires
`hostPID`, host log/sysfs/device mounts, and a privileged container. If your
cluster policy forbids those permissions, install the native Ubuntu package on
the nodes instead.

See [platform support](../docs/platform-support.md) for the complete support
matrix and limitations.
