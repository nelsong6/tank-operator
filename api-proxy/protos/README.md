# Envoy ext_proc proto compilation

The runtime image needs the `envoy.service.ext_proc.v3.ExternalProcessor`
gRPC stubs plus their transitive dependencies. We don't vendor the .proto
tree into git — it's too large and stale fast. Instead, the Dockerfile's
build stage clones the upstream proto sources at pinned refs and runs
`grpc_tools.protoc` over them.

Pinned refs (Dockerfile keeps these in sync; this file is the human readme):

  envoyproxy/envoy        v1.34.0   (envoy/api/ + envoy/type/ proto trees)
  cncf/xds                main @ pinned commit
  bufbuild/protoc-gen-validate  v1.0.4
  googleapis/googleapis   master @ pinned commit (googleapis tree)
  cncf/udpa               main @ pinned commit
  census-instrumentation/opencensus-proto  v0.4.1

Why these specifically: envoy's data-plane-api references all six in its
Bazel `repository_locations.bzl`. Compiling `external_processor.proto`
transitively pulls in:
  - envoy/config/core/v3/base.proto
  - envoy/extensions/filters/http/ext_proc/v3/processing_mode.proto
  - envoy/type/v3/http_status.proto
  - validate/validate.proto              (protoc-gen-validate)
  - xds/annotations/v3/status.proto       (cncf/xds)
  - udpa/annotations/{status,versioning}.proto  (cncf/udpa)
  - google/api/{annotations,http,field_behavior}.proto  (googleapis)
  - opencensus/proto/trace/v1/trace.proto (only via deep deps; harmless)

To bump: change the refs in `api-proxy/Dockerfile` and rebuild. If the
build fails with "proto X imports proto Y, file not found", the dep
graph drifted upstream — add the missing repo to the Dockerfile.
