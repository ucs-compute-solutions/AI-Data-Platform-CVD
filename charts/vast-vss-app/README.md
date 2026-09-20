# VAST VSS application Helm chart

This chart packages the OpenShift application layer used by the pinned
VAST-native VSS companion. It creates the application Deployments, Services,
Route, ServiceAccount, and optional source-service resources. It does not
create Secret values or VAST DataEngine resources.

The workload wrapper renders this chart with site-specific, digest-pinned
images:

```bash
cd workloads/vast-native-vss
./scripts/render.sh --env release-inputs.env
```

Use [`../../workloads/vast-native-vss/README.md`](../../workloads/vast-native-vss/README.md)
as the deployment entry point. Rendering or linting the chart does not prove
that referenced images, Secrets, model services, or VAST resources exist.

The chart includes the validated OpenShift runtime paths, upload-size and
request-timeout settings, and optional streaming, batch-sync, and retrieval
adapter templates. The base CVD workflow keeps those optional services
disabled until their images and data paths are separately reviewed.
