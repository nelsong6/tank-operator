# syntax=docker/dockerfile:1.7
#
# Two-stage build:
#  1. proto-build: clones envoy/api + its transitive proto deps at pinned
#     refs and compiles them into Python modules under /out/site-packages.
#     See protos/README.md for the dependency-graph rationale.
#  2. runtime: slim image, pip-installs the proxy package, copies the
#     pre-compiled protos into site-packages, drops privileges, runs the
#     gRPC server on :9000.

# ---------------------------------------------------------------------------
# Stage 1: compile envoy ext_proc protos
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS proto-build

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir grpcio-tools==1.66.2

WORKDIR /work

# Pinned refs. Bumping any of these is a chart-image rebuild; nothing
# downstream depends on the version directly.
ARG ENVOY_REF=v1.34.0
ARG XDS_REF=main
ARG PGV_REF=v1.0.4
ARG GOOGLEAPIS_REF=master
ARG UDPA_REF=main
ARG OPENCENSUS_REF=v0.4.1

RUN git clone --depth=1 --branch ${ENVOY_REF}      https://github.com/envoyproxy/envoy.git                envoy \
 && git clone --depth=1 --branch ${XDS_REF}        https://github.com/cncf/xds.git                         xds \
 && git clone --depth=1 --branch ${PGV_REF}        https://github.com/bufbuild/protoc-gen-validate.git    pgv \
 && git clone --depth=1 --branch ${GOOGLEAPIS_REF} https://github.com/googleapis/googleapis.git           googleapis \
 && git clone --depth=1 --branch ${UDPA_REF}       https://github.com/cncf/udpa.git                        udpa \
 && git clone --depth=1 --branch ${OPENCENSUS_REF} https://github.com/census-instrumentation/opencensus-proto.git opencensus

# protoc-gen-validate's proto lives at validate/validate.proto in repo
# root; arrange a flat include path that protoc can find.
RUN mkdir -p /work/include \
 && cp -r envoy/api/envoy            /work/include/envoy \
 && cp -r xds/xds                    /work/include/xds \
 && cp -r pgv/validate               /work/include/validate \
 && cp -r udpa/udpa                  /work/include/udpa \
 && cp -r opencensus/src/opencensus  /work/include/opencensus \
 && mkdir -p /work/include/google \
 && cp -r googleapis/google/api      /work/include/google/api \
 && cp -r googleapis/google/rpc      /work/include/google/rpc

# Pull google/protobuf/*.proto from grpcio-tools' bundled copy so we don't
# have to chase another upstream repo.
RUN PROTOC_PROTOS=$(python -c "import grpc_tools, os; print(os.path.join(os.path.dirname(grpc_tools.__file__), '_proto'))") \
 && cp -r $PROTOC_PROTOS/google/protobuf /work/include/google/protobuf

WORKDIR /work/include

# Compile only the proto subtrees we actually import. The googleapis
# top-level tree is intentionally pruned to api/+rpc/+protobuf/ — the
# rest of googleapis (BigQuery, Vision, etc.) drags in deps we don't
# have (grafeas) and we don't import any of it from envoy.
RUN mkdir -p /out \
 && find envoy xds validate google udpa opencensus -name '*.proto' -print0 \
    | xargs -0 python -m grpc_tools.protoc \
        -I /work/include \
        --python_out=/out \
        --grpc_python_out=/out

# Generate __init__.py at every package level so the protobuf modules are
# importable as regular packages (protoc emits files but not __init__.py).
RUN find /out -type d -exec touch {}/__init__.py \;

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN groupadd --system --gid 65532 apiproxy \
 && useradd --system --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin apiproxy

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# Drop the compiled envoy + deps protos into site-packages so
#   `from envoy.service.ext_proc.v3 import external_processor_pb2`
# resolves at runtime.
COPY --from=proto-build /out/ /usr/local/lib/python3.12/site-packages/

USER apiproxy
EXPOSE 9000

CMD ["python", "-m", "tank_api_proxy"]
