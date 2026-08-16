package dev.foliasmp.routessync;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Merges every configured cluster's own {@code domains} map (PLAN.md §7C,
 * validated for uniqueness only within that one mgmt's inventory) into one
 * flat domain -> qualified-server-name map this proxy instance's
 * {@link VirtualHostRouter} can resolve against (PLAN.md §7D). Pure
 * logic, directly unit-testable.
 *
 * <p>Two independent clusters' operators could genuinely claim the same
 * public domain by mistake — each mgmt only validates uniqueness against
 * its own hosts, since mgmt instances don't know about each other. This
 * can't prevent that, but it resolves it deterministically (first cluster
 * in {@code clusterIdsInPriorityOrder} wins) and reports every collision
 * rather than silently flip-flopping between polls.
 */
public final class DomainRouteMerger {

    public record Result(Map<String, String> domainRoutes, List<String> collisions) {
    }

    private DomainRouteMerger() {
    }

    public static Result merge(List<String> clusterIdsInPriorityOrder, Map<String, Map<String, String>> perClusterDomains) {
        Map<String, String> merged = new HashMap<>();
        Map<String, String> owner = new HashMap<>();
        List<String> collisions = new ArrayList<>();

        for (String clusterId : clusterIdsInPriorityOrder) {
            Map<String, String> domains = perClusterDomains.getOrDefault(clusterId, Map.of());
            for (Map.Entry<String, String> entry : domains.entrySet()) {
                String domain = entry.getKey();
                String existingOwner = owner.get(domain);
                if (existingOwner != null) {
                    collisions.add(domain + " is claimed by both cluster '" + existingOwner
                            + "' and cluster '" + clusterId + "' — keeping '" + existingOwner + "'");
                    continue;
                }
                owner.put(domain, clusterId);
                merged.put(domain, ServerName.qualify(clusterId, entry.getValue()));
            }
        }

        return new Result(merged, collisions);
    }
}
