"""The names the kernel injects into `repld`, and the error for reading one anywhere else."""

# Injected by the kernel (`kernel._inject_builtins`, `browser`); absent anywhere else (`browser` until `repld.browser` is imported).
_KERNEL_ONLY = frozenset(
    {
        "notify",
        "defer",
        "every",
        "ask",
        "confirm",
        "choose",
        "form",
        "GateDeclined",
        "no_display",
        "claude_sessions",
        "current_session_id",
        "peer_caller",
        "peers",
        "browser",
    }
)


class NotInKernelError(AttributeError):
    """A kernel-only builtin was read outside a kernel; subclasses `AttributeError` so `hasattr` still answers False."""
