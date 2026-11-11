# Hub adapter build targets. Follows the repo convention of working in
# ephemeral environments; all targets are plain Go toolchain invocations.

BINARY ?= bin/hub-adapter

.PHONY: build test vet fmt lint all clean

build:
	go build -o $(BINARY) ./cmd

test:
	go test ./...

vet:
	go vet ./...

fmt:
	gofmt -l .

# Fix formatting in place.
fmt-fix:
	gofmt -w .

# All gates expected green before review.
all: build test vet fmt

clean:
	rm -rf bin
