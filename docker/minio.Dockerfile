# MinIO built from source (MyCookie/minio fork); the pinned release is env.VERSION in
# .github/workflows/minio-image.yml.
# Upstream is archived and publishes no binaries or images; see #52.
# Risk accepted for LOCAL and EPHEMERAL CI USE ONLY. NEVER use in production
# (production uses cloud object storage, docs/spec/05 §3).
# Build context: a checkout of the minio source at the pinned commit.
# golang:1.24-bookworm, multi-arch index digest looked up 2026-11-10; bump both digests together with their tags.
FROM --platform=$BUILDPLATFORM golang:1.24-bookworm@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac AS build
ARG TARGETOS
ARG TARGETARCH
# VERSION and COMMIT come only from the workflow env (.github/workflows/minio-image.yml).
ARG VERSION
ARG COMMIT
ENV GOTOOLCHAIN=local CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH
WORKDIR /src
COPY . .
RUN : "${VERSION:?build-arg VERSION required}" "${COMMIT:?build-arg COMMIT required}" && TAG=$(echo "$VERSION" | tr : -) && go build -tags kqueue -trimpath -o /minio -ldflags "-s -w \
 -X github.com/minio/minio/cmd.Version=${VERSION} \
 -X github.com/minio/minio/cmd.CopyrightYear=2025 \
 -X github.com/minio/minio/cmd.ReleaseTag=RELEASE.${TAG} \
 -X github.com/minio/minio/cmd.CommitID=${COMMIT} \
 -X github.com/minio/minio/cmd.ShortCommitID=${COMMIT%${COMMIT#????????????}}"

# alpine:3.23, multi-arch index digest looked up 2026-11-10.
FROM alpine:3.23@sha256:469c9ea410b9215eaee7976ba6c0639039b444be8e5d638f7abbada189971eb2
LABEL org.opencontainers.image.source=https://github.com/MyCookie/tuner
# curl: the compose healthcheck runs `curl -f http://localhost:9000/minio/health/live`
RUN apk add --no-cache curl ca-certificates
# Non-root (docs/spec/05 Security). Fixed uid so volume ownership is predictable; /data is created
# and chowned before VOLUME so a fresh named volume inherits it.
RUN addgroup -g 10001 -S minio && adduser -S -u 10001 -G minio -H -s /sbin/nologin minio \
 && mkdir /data && chown minio:minio /data
COPY --from=build /minio /usr/bin/minio
EXPOSE 9000 9001
VOLUME /data
USER minio
ENTRYPOINT ["minio"]
