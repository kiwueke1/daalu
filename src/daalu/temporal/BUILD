# src/daalu/temporal/BUILD

load("@rules_python//python:defs.bzl", "py_library", "py_binary")

py_library(
    name = "temporal",
    srcs = [
        "workflows.py",
        "activities.py",
        "client.py",
        "settings.py",
        "models.py",
    ],
    deps = [
        "//src/daalu:daalu",
        "@pip//temporalio:pkg",
        "@pip//pyyaml:pkg",
    ],
    visibility = [
        "//src/daalu:__pkg__",   # allow src/daalu (cli_app) to depend on this
    ],
)

py_binary(
    name = "worker",
    srcs = [
        "worker.py",
    ],
    deps = [
        ":temporal",
    ],
)
