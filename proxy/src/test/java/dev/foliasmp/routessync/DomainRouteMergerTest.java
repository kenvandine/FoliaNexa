package dev.foliasmp.routessync;

import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class DomainRouteMergerTest {

    @Test
    void mergesDisjointClusterDomains() {
        Map<String, Map<String, String>> perCluster = new LinkedHashMap<>();
        perCluster.put("domain1", Map.of("play.domain1.com", "lobby"));
        perCluster.put("domain2", Map.of("play.domain2.com", "lobby"));

        DomainRouteMerger.Result result = DomainRouteMerger.merge(List.of("domain1", "domain2"), perCluster);

        assertEquals(
                Map.of("play.domain1.com", "domain1__lobby", "play.domain2.com", "domain2__lobby"),
                result.domainRoutes());
        assertTrue(result.collisions().isEmpty());
    }

    @Test
    void firstClusterInPriorityOrderWinsACollision() {
        Map<String, Map<String, String>> perCluster = new LinkedHashMap<>();
        perCluster.put("domain1", Map.of("play.shared.com", "lobby-a"));
        perCluster.put("domain2", Map.of("play.shared.com", "lobby-b"));

        DomainRouteMerger.Result result = DomainRouteMerger.merge(List.of("domain1", "domain2"), perCluster);

        assertEquals(Map.of("play.shared.com", "domain1__lobby-a"), result.domainRoutes());
        assertEquals(1, result.collisions().size());
        assertTrue(result.collisions().get(0).contains("domain1"));
        assertTrue(result.collisions().get(0).contains("domain2"));
    }

    @Test
    void clusterWithNoDomainsContributesNothing() {
        Map<String, Map<String, String>> perCluster = Map.of("domain1", Map.of());
        DomainRouteMerger.Result result = DomainRouteMerger.merge(List.of("domain1"), perCluster);
        assertTrue(result.domainRoutes().isEmpty());
        assertTrue(result.collisions().isEmpty());
    }

    @Test
    void unknownClusterIdInPriorityOrderIsHarmless() {
        DomainRouteMerger.Result result = DomainRouteMerger.merge(List.of("ghost"), Map.of());
        assertTrue(result.domainRoutes().isEmpty());
    }
}
