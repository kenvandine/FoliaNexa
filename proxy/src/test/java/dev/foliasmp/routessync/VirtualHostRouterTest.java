package dev.foliasmp.routessync;

import org.junit.jupiter.api.Test;

import java.util.Map;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class VirtualHostRouterTest {

    private static final Map<String, String> DOMAIN_ROUTES = Map.of(
            "smp.example.com", "world-a-lobby",
            "creative.example.com", "world-b-lobby"
    );

    @Test
    void matchedDomainWinsOverDefault() {
        Optional<String> result = VirtualHostRouter.resolve(DOMAIN_ROUTES, Optional.of("smp.example.com"), "world-global-default");
        assertEquals(Optional.of("world-a-lobby"), result);
    }

    @Test
    void unmatchedDomainFallsBackToDefault() {
        Optional<String> result = VirtualHostRouter.resolve(DOMAIN_ROUTES, Optional.of("unknown.example.com"), "world-global-default");
        assertEquals(Optional.of("world-global-default"), result);
    }

    @Test
    void noVirtualHostFallsBackToDefault() {
        // Bedrock/Geyser connections and legacy Java pings carry no
        // hostname at all — RakNet has no SNI concept (PLAN.md §7C).
        Optional<String> result = VirtualHostRouter.resolve(DOMAIN_ROUTES, Optional.empty(), "world-global-default");
        assertEquals(Optional.of("world-global-default"), result);
    }

    @Test
    void noMatchAndNoDefaultResolvesEmpty() {
        Optional<String> result = VirtualHostRouter.resolve(DOMAIN_ROUTES, Optional.empty(), null);
        assertTrue(result.isEmpty());
    }

    @Test
    void matchIsCaseInsensitive() {
        Optional<String> result = VirtualHostRouter.resolve(DOMAIN_ROUTES, Optional.of("SMP.Example.COM"), "world-global-default");
        assertEquals(Optional.of("world-a-lobby"), result);
    }

    @Test
    void matchStripsTrailingDot() {
        Optional<String> result = VirtualHostRouter.resolve(DOMAIN_ROUTES, Optional.of("smp.example.com."), "world-global-default");
        assertEquals(Optional.of("world-a-lobby"), result);
    }

    @Test
    void emptyDomainRoutesMapAlwaysFallsBackToDefault() {
        Optional<String> result = VirtualHostRouter.resolve(Map.of(), Optional.of("smp.example.com"), "world-global-default");
        assertEquals(Optional.of("world-global-default"), result);
    }
}
