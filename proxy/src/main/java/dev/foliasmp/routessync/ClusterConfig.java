package dev.foliasmp.routessync;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * Parses this plugin's mgmt-cluster configuration from environment
 * variables (PLAN.md §7D). Pure logic, no Velocity dependency, directly
 * unit-testable — same style as {@link RouteDiff}/{@link AccessDecision}.
 *
 * <p>Two shapes, chosen so an existing single-cluster deployment (PLAN.md
 * §7A/§7C) needs zero config changes to keep working:
 *
 * <ul>
 *   <li><b>Single-cluster (legacy)</b>: just {@code FOLIA_MGMT_URL} /
 *       {@code FOLIA_MGMT_API_TOKEN}, exactly as before this feature
 *       existed — becomes one cluster named {@code "default"}.</li>
 *   <li><b>Multi-cluster</b>: {@code FOLIA_MGMT_CLUSTER_IDS} (comma- or
 *       whitespace-separated list of short ids, e.g.
 *       {@code "domain1,domain2"}) plus, per id,
 *       {@code FOLIA_MGMT_CLUSTER_<ID>_URL} and
 *       {@code FOLIA_MGMT_CLUSTER_<ID>_TOKEN} (id upper-cased, hyphens
 *       turned into underscores, for the env var name).
 *       {@code FOLIA_MGMT_PRIMARY_CLUSTER} picks which cluster's default
 *       world backstops connections that carry no hostname at all
 *       (Bedrock/RakNet — no SNI concept — or a bare-IP Java connection);
 *       defaults to the first id listed.</li>
 * </ul>
 *
 * One VPS-edge proxy instance polls every configured cluster's own
 * independent mgmt (PLAN.md §7D) — each cluster is a fully separate
 * FoliaNexa deployment (own mgmt process, own database, reachable over
 * its own WireGuard tunnel peer), which is a different, broader thing
 * than PLAN.md §7C's per-host {@code Host.domains} (multiple hosts
 * sharing *one* mgmt's inventory).
 */
public final class ClusterConfig {
    private static final Pattern ID_PATTERN = Pattern.compile("[a-zA-Z0-9_-]+");
    private static final Pattern SPLIT = Pattern.compile("[,\\s]+");

    public final String id;
    public final String mgmtUrl;
    public final String apiToken;

    public ClusterConfig(String id, String mgmtUrl, String apiToken) {
        this.id = id;
        this.mgmtUrl = mgmtUrl;
        this.apiToken = apiToken;
    }

    public record Parsed(List<ClusterConfig> clusters, String primaryClusterId) {
    }

    public static Parsed parse(Map<String, String> env) {
        String idsRaw = env.get("FOLIA_MGMT_CLUSTER_IDS");
        if (idsRaw == null || idsRaw.isBlank()) {
            ClusterConfig legacy = new ClusterConfig("default", require(env, "FOLIA_MGMT_URL"), require(env, "FOLIA_MGMT_API_TOKEN"));
            return new Parsed(List.of(legacy), "default");
        }

        List<String> ids = new ArrayList<>();
        for (String rawId : SPLIT.split(idsRaw.trim())) {
            if (rawId.isBlank()) {
                continue;
            }
            if (!ID_PATTERN.matcher(rawId).matches()) {
                throw new IllegalStateException(
                        "FOLIA_MGMT_CLUSTER_IDS entry '" + rawId + "' must contain only letters, digits, '-' or '_'");
            }
            ids.add(rawId);
        }
        if (ids.isEmpty()) {
            throw new IllegalStateException("FOLIA_MGMT_CLUSTER_IDS is set but has no entries");
        }

        Map<String, ClusterConfig> byId = new LinkedHashMap<>();
        for (String id : ids) {
            if (byId.containsKey(id)) {
                throw new IllegalStateException("duplicate cluster id '" + id + "' in FOLIA_MGMT_CLUSTER_IDS");
            }
            String envPrefix = "FOLIA_MGMT_CLUSTER_" + id.toUpperCase(Locale.ROOT).replace('-', '_');
            byId.put(id, new ClusterConfig(id, require(env, envPrefix + "_URL"), require(env, envPrefix + "_TOKEN")));
        }

        String primary = env.get("FOLIA_MGMT_PRIMARY_CLUSTER");
        if (primary == null || primary.isBlank()) {
            primary = ids.get(0);
        } else if (!byId.containsKey(primary)) {
            throw new IllegalStateException(
                    "FOLIA_MGMT_PRIMARY_CLUSTER '" + primary + "' is not one of FOLIA_MGMT_CLUSTER_IDS " + ids);
        }

        return new Parsed(new ArrayList<>(byId.values()), primary);
    }

    private static String require(Map<String, String> env, String name) {
        String value = env.get(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException(name + " must be set (see FoliaRoutesSyncPlugin javadoc)");
        }
        return value;
    }
}
