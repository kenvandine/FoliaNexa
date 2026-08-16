package dev.foliasmp.routessync;

import java.util.Optional;

/**
 * Qualifies a world name with its owning cluster id, so that when this
 * proxy polls multiple independent mgmt clusters (PLAN.md §7D), two
 * clusters' same-named worlds (e.g. both running a "world-overworld")
 * never collide in Velocity's single global server registry. Pure
 * string logic, directly unit-testable.
 */
public final class ServerName {
    private static final String SEPARATOR = "__";

    private ServerName() {
    }

    public static String qualify(String clusterId, String worldName) {
        return clusterId + SEPARATOR + worldName;
    }

    /** The cluster id portion of a qualified name, or empty if it isn't one. */
    public static Optional<String> clusterIdOf(String qualifiedName) {
        int idx = qualifiedName.indexOf(SEPARATOR);
        return idx < 0 ? Optional.empty() : Optional.of(qualifiedName.substring(0, idx));
    }

    /** The original (unqualified) world name portion, or empty if it isn't a qualified name. */
    public static Optional<String> worldNameOf(String qualifiedName) {
        int idx = qualifiedName.indexOf(SEPARATOR);
        return idx < 0 ? Optional.empty() : Optional.of(qualifiedName.substring(idx + SEPARATOR.length()));
    }
}
