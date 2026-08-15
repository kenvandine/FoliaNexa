package dev.foliasmp.routessync;

import java.util.Set;
import java.util.UUID;

/**
 * Pure allow/deny logic, kept separate from the Velocity event wiring so
 * it's directly unit-testable. PLAN.md §11C.
 */
public final class AccessDecision {
    private AccessDecision() {
    }

    public static boolean isAllowed(boolean gateEnabled, Set<UUID> approvedUuids, UUID playerUuid) {
        if (!gateEnabled) {
            return true; // gate is opt-in — see FoliaRoutesSyncPlugin's FOLIA_ACCESS_GATE_ENABLED
        }
        return approvedUuids.contains(playerUuid);
    }
}
