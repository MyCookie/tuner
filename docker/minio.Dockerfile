# MinIO RELEASE.2025-04-22T22-12-26Z built from source (MyCookie/minio fork).
# Upstream is archived and publishes no binaries or images; see #52.
# Risk accepted for LOCAL and EPHEMERAL CI USE ONLY. NEVER use in production
# (production uses cloud object storage, docs/spec/05 §3).
# Build context: a checkout of the minio source at the pinned commit.
FROM --platform=$BUILDPLATFORM golang:1.24-bookworm AS build
ARG TARGETOS
ARG TARGETARCH
ARG VERSION=2025-04-22T22:12:26Z
ARG COMMIT=0d7408fc9969caf07de6a8c3a84f9fbb10a6739e
ENV GOTOOLCHAIN=local CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH
WORKDIR /src
COPY . .
RUN TAG=$(echo "$VERSION" | tr : -) && go build -tags kqueue -trimpath -o /minio -ldflags "-s -w \
 -X github.com/minio/minio/cmd.Version=${VERSION} \
 -X github.com/minio/minio/cmd.CopyrightYear=2025 \
 -X github.com/minio/minio/cmd.ReleaseTag=RELEASE.${TAG} \
 -X github.com/minio/minio/cmd.CommitID=${COMMIT} \
 -X github.com/minio/minio/cmd.ShortCommitID=${COMMIT%${COMMIT#????????????}}"

FROM alpine:3.23
LABEL org.opencontainers.image.source=https://github.com/MyCookie/tuner
# curl: the compose healthcheck runs `curl -f http://localhost:9000/minio/health/live`
RUN apk add --no-cache curl ca-certificates
COPY --from=build /minio /usr/bin/minio
EXPOSE 9000 9001
VOLUME /data
ENTRYPOINT ["minio"]
