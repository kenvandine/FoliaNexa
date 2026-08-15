package dev.foliasmp.routessync;

import java.util.Objects;

/**
 * A single entry from mgmt's {@code GET /api/v1/routes} (PLAN.md §7).
 */
public record Route(String world, String type, String host, int port, boolean isDefault) {
    public Route {
        Objects.requireNonNull(world, "world");
        Objects.requireNonNull(host, "host");
    }

    public String address() {
        return host + ":" + port;
    }
}
