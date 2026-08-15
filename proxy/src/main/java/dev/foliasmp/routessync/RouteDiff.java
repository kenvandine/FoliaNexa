package dev.foliasmp.routessync;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * Pure diffing logic — no Velocity/network dependency, so it's directly
 * unit-testable. PLAN.md §7: "reconciles its own server-list against
 * [mgmt's routes] — no restart required to add or remove a world."
 */
public final class RouteDiff {

    public record Plan(List<Route> toRegister, List<String> toUnregister, Optional<String> defaultWorld) {
    }

    private RouteDiff() {
    }

    /**
     * @param currentlyRegistered world name -> "host:port" of what's registered with the proxy right now
     * @param desired              the latest routes from mgmt
     */
    public static Plan compute(Map<String, String> currentlyRegistered, List<Route> desired) {
        List<Route> toRegister = new ArrayList<>();
        Set<String> desiredNames = new HashSet<>();
        Optional<String> defaultWorld = Optional.empty();

        for (Route route : desired) {
            desiredNames.add(route.world());
            String currentAddress = currentlyRegistered.get(route.world());
            // Covers both "not registered yet" and "registered at a stale
            // address" (world moved hosts) — the caller unregisters the
            // stale ServerInfo before re-registering either way, since
            // Velocity keys registration by name and won't let you just
            // overwrite one in place.
            if (currentAddress == null || !currentAddress.equals(route.address())) {
                toRegister.add(route);
            }
            if (route.isDefault()) {
                defaultWorld = Optional.of(route.world());
            }
        }

        List<String> toUnregister = new ArrayList<>();
        for (String registeredName : currentlyRegistered.keySet()) {
            if (!desiredNames.contains(registeredName)) {
                toUnregister.add(registeredName);
            }
        }

        return new Plan(toRegister, toUnregister, defaultWorld);
    }
}
