package dev.foliasmp.routessync;

import java.util.Locale;
import java.util.Map;
import java.util.Optional;

/**
 * Pure hostname-routing logic — no Velocity dependency, directly
 * unit-testable, same style as {@link RouteDiff} and {@link AccessDecision}.
 * PLAN.md §7C: a domain a player connected on resolves to the world that's
 * the default for whichever host declared that domain; anything unmatched
 * (including Bedrock connections, which carry no hostname at all — RakNet
 * has no SNI/hostname concept) falls back to the cluster's single global
 * default world, exactly as it always has.
 */
public final class VirtualHostRouter {

    private VirtualHostRouter() {
    }

    public static Optional<String> resolve(
            Map<String, String> domainRoutes, Optional<String> virtualHost, String defaultWorld) {
        String matched = virtualHost.map(VirtualHostRouter::normalize).map(domainRoutes::get).orElse(null);
        return Optional.ofNullable(matched != null ? matched : defaultWorld);
    }

    static String normalize(String host) {
        String normalized = host.toLowerCase(Locale.ROOT);
        if (normalized.endsWith(".")) {
            normalized = normalized.substring(0, normalized.length() - 1);
        }
        return normalized;
    }
}
