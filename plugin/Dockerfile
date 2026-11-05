FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go test -race ./... && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/tracearr-bridge-linux-amd64 .
RUN /out/tracearr-bridge-linux-amd64 manifest > /out/manifest.json
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/repository-tool ./cmd/repository-tool
WORKDIR /out
RUN sha256sum tracearr-bridge-linux-amd64 > SHA256SUMS

FROM scratch AS artifact
COPY --from=build /out/ /
