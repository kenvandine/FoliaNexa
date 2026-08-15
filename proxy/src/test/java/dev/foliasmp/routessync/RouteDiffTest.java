package dev.foliasmp.routessync;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RouteDiffTest {

    @Test
    void newRouteIsRegisteredAndNotUnregistered() {
        var plan = RouteDiff.compute(Map.of(), List.of(new Route("world-lobby", "lobby", "10.0.2.10", 25565, false)));

        assertEquals(1, plan.toRegister().size());
        assertEquals("world-lobby", plan.toRegister().get(0).world());
        assertTrue(plan.toUnregister().isEmpty());
    }

    @Test
    void unchangedRouteIsNotReRegistered() {
        var current = Map.of("world-lobby", "10.0.2.10:25565");
        var desired = List.of(new Route("world-lobby", "lobby", "10.0.2.10", 25565, false));

        var plan = RouteDiff.compute(current, desired);

        assertTrue(plan.toRegister().isEmpty());
        assertTrue(plan.toUnregister().isEmpty());
    }

    @Test
    void changedAddressIsReRegisteredNotUnregistered() {
        var current = Map.of("world-nether", "10.0.1.22:25565");
        var desired = List.of(new Route("world-nether", "nether", "10.0.1.99", 25565, false));

        var plan = RouteDiff.compute(current, desired);

        assertEquals(1, plan.toRegister().size());
        assertEquals("10.0.1.99", plan.toRegister().get(0).host());
        // it's a re-register, not a fresh add, so it must NOT also show up
        // as something to tear down (the caller handles unregister-old
        // itself for this case, since RouteDiff only tracks the
        // *disappeared* set here)
        assertTrue(plan.toUnregister().isEmpty());
    }

    @Test
    void removedRouteIsUnregistered() {
        var current = Map.of("world-old-minigame", "10.0.1.50:25565");
        var plan = RouteDiff.compute(current, List.of());

        assertEquals(List.of("world-old-minigame"), plan.toUnregister());
        assertTrue(plan.toRegister().isEmpty());
    }

    @Test
    void defaultWorldIsIdentified() {
        var desired = List.of(
                new Route("world-nether", "nether", "10.0.1.22", 25565, false),
                new Route("world-overworld", "overworld", "10.0.1.21", 25565, true)
        );

        var plan = RouteDiff.compute(Map.of(), desired);

        assertEquals("world-overworld", plan.defaultWorld().orElseThrow());
    }

    @Test
    void noDefaultWorldWhenNoneFlagged() {
        var desired = List.of(new Route("world-nether", "nether", "10.0.1.22", 25565, false));
        var plan = RouteDiff.compute(Map.of(), desired);
        assertTrue(plan.defaultWorld().isEmpty());
    }

    @Test
    void mixedAddDropAndKeepInOnePass() {
        var current = Map.of(
                "world-overworld", "10.0.1.21:25565", // unchanged
                "world-old-minigame", "10.0.1.50:25565" // dropped
        );
        var desired = List.of(
                new Route("world-overworld", "overworld", "10.0.1.21", 25565, true), // unchanged
                new Route("world-lobby", "lobby", "10.0.2.10", 25565, false) // new
        );

        var plan = RouteDiff.compute(current, desired);

        assertEquals(1, plan.toRegister().size());
        assertEquals("world-lobby", plan.toRegister().get(0).world());
        assertEquals(List.of("world-old-minigame"), plan.toUnregister());
    }
}
