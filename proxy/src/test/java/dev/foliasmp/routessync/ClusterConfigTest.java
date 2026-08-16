package dev.foliasmp.routessync;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class ClusterConfigTest {

    @Test
    void legacySingleClusterFromFoliaMgmtUrl() {
        Map<String, String> env = Map.of(
                "FOLIA_MGMT_URL", "https://mgmt.internal:8443",
                "FOLIA_MGMT_API_TOKEN", "secret-token"
        );

        ClusterConfig.Parsed parsed = ClusterConfig.parse(env);

        assertEquals(1, parsed.clusters().size());
        assertEquals("default", parsed.clusters().get(0).id);
        assertEquals("https://mgmt.internal:8443", parsed.clusters().get(0).mgmtUrl);
        assertEquals("secret-token", parsed.clusters().get(0).apiToken);
        assertEquals("default", parsed.primaryClusterId());
    }

    @Test
    void missingLegacyEnvVarsThrows() {
        assertThrows(IllegalStateException.class, () -> ClusterConfig.parse(Map.of()));
    }

    @Test
    void multiClusterFromClusterIds() {
        Map<String, String> env = Map.of(
                "FOLIA_MGMT_CLUSTER_IDS", "domain1,domain2",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_URL", "https://mgmt1.internal:8443",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_TOKEN", "token1",
                "FOLIA_MGMT_CLUSTER_DOMAIN2_URL", "https://mgmt2.internal:8443",
                "FOLIA_MGMT_CLUSTER_DOMAIN2_TOKEN", "token2"
        );

        ClusterConfig.Parsed parsed = ClusterConfig.parse(env);

        assertEquals(2, parsed.clusters().size());
        assertEquals("domain1", parsed.clusters().get(0).id);
        assertEquals("https://mgmt1.internal:8443", parsed.clusters().get(0).mgmtUrl);
        assertEquals("domain2", parsed.clusters().get(1).id);
        assertEquals("https://mgmt2.internal:8443", parsed.clusters().get(1).mgmtUrl);
        // No FOLIA_MGMT_PRIMARY_CLUSTER set — first listed id wins.
        assertEquals("domain1", parsed.primaryClusterId());
    }

    @Test
    void clusterIdsAcceptWhitespaceSeparators() {
        Map<String, String> env = Map.of(
                "FOLIA_MGMT_CLUSTER_IDS", "domain1  domain2",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_URL", "https://mgmt1.internal:8443",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_TOKEN", "token1",
                "FOLIA_MGMT_CLUSTER_DOMAIN2_URL", "https://mgmt2.internal:8443",
                "FOLIA_MGMT_CLUSTER_DOMAIN2_TOKEN", "token2"
        );

        assertEquals(2, ClusterConfig.parse(env).clusters().size());
    }

    @Test
    void explicitPrimaryClusterIsHonored() {
        Map<String, String> env = Map.of(
                "FOLIA_MGMT_CLUSTER_IDS", "domain1,domain2",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_URL", "https://mgmt1.internal:8443",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_TOKEN", "token1",
                "FOLIA_MGMT_CLUSTER_DOMAIN2_URL", "https://mgmt2.internal:8443",
                "FOLIA_MGMT_CLUSTER_DOMAIN2_TOKEN", "token2",
                "FOLIA_MGMT_PRIMARY_CLUSTER", "domain2"
        );

        assertEquals("domain2", ClusterConfig.parse(env).primaryClusterId());
    }

    @Test
    void hyphenatedClusterIdMapsToUnderscoredEnvVarPrefix() {
        Map<String, String> env = Map.of(
                "FOLIA_MGMT_CLUSTER_IDS", "my-domain",
                "FOLIA_MGMT_CLUSTER_MY_DOMAIN_URL", "https://mgmt.internal:8443",
                "FOLIA_MGMT_CLUSTER_MY_DOMAIN_TOKEN", "token"
        );

        ClusterConfig.Parsed parsed = ClusterConfig.parse(env);
        assertEquals("my-domain", parsed.clusters().get(0).id);
    }

    @Test
    void unknownPrimaryClusterThrows() {
        Map<String, String> env = Map.of(
                "FOLIA_MGMT_CLUSTER_IDS", "domain1",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_URL", "https://mgmt1.internal:8443",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_TOKEN", "token1",
                "FOLIA_MGMT_PRIMARY_CLUSTER", "ghost"
        );

        assertThrows(IllegalStateException.class, () -> ClusterConfig.parse(env));
    }

    @Test
    void duplicateClusterIdThrows() {
        Map<String, String> env = Map.of(
                "FOLIA_MGMT_CLUSTER_IDS", "domain1,domain1",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_URL", "https://mgmt1.internal:8443",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_TOKEN", "token1"
        );

        assertThrows(IllegalStateException.class, () -> ClusterConfig.parse(env));
    }

    @Test
    void invalidClusterIdCharacterThrows() {
        Map<String, String> env = Map.of("FOLIA_MGMT_CLUSTER_IDS", "domain.one");
        assertThrows(IllegalStateException.class, () -> ClusterConfig.parse(env));
    }

    @Test
    void missingUrlForAConfiguredClusterThrows() {
        Map<String, String> env = Map.of(
                "FOLIA_MGMT_CLUSTER_IDS", "domain1",
                "FOLIA_MGMT_CLUSTER_DOMAIN1_TOKEN", "token1"
        );

        assertThrows(IllegalStateException.class, () -> ClusterConfig.parse(env));
    }
}
