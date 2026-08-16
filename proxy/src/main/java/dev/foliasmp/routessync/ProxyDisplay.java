package dev.foliasmp.routessync;

import java.util.Objects;

/**
 * The server-list MOTD/icon fetched from {@code GET /api/v1/routes/display}
 * (PLAN.md §7) — editable from the mgmt dashboard, applied live via
 * {@link FoliaRoutesSyncPlugin#onProxyPing}, no proxy restart needed.
 */
public record ProxyDisplay(String motd, String iconBase64) {
    public ProxyDisplay {
        Objects.requireNonNull(motd, "motd");
    }
}
