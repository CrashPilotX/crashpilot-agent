# Kubernetes node agent

The CrashPilot DaemonSet deploys one privileged node-forensics agent per Linux
node.

```bash
cp k8s/secret.example.yaml /tmp/crashpilot-secret.yaml
# Edit /tmp/crashpilot-secret.yaml with the connection values from the dashboard.
kubectl apply -f /tmp/crashpilot-secret.yaml
kubectl apply -f k8s/daemonset.yaml
kubectl rollout status daemonset/crashpilot-agent -n crashpilot
```

The deployment is tested in Kind on every relevant change. It requires
`hostPID`, host log/sysfs/device mounts, and a privileged container. If your
cluster policy forbids those permissions, install the native Ubuntu package on
the nodes instead.

See [platform support](../docs/platform-support.md) for the complete support
matrix and limitations.
