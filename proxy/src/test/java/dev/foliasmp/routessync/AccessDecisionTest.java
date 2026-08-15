package dev.foliasmp.routessync;

import org.junit.jupiter.api.Test;

import java.util.Set;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class AccessDecisionTest {

    private static final UUID APPROVED = UUID.randomUUID();
    private static final UUID NOT_APPROVED = UUID.randomUUID();

    @Test
    void everyoneAllowedWhenGateDisabled() {
        assertTrue(AccessDecision.isAllowed(false, Set.of(), NOT_APPROVED));
    }

    @Test
    void approvedPlayerAllowedWhenGateEnabled() {
        assertTrue(AccessDecision.isAllowed(true, Set.of(APPROVED), APPROVED));
    }

    @Test
    void unapprovedPlayerDeniedWhenGateEnabled() {
        assertFalse(AccessDecision.isAllowed(true, Set.of(APPROVED), NOT_APPROVED));
    }

    @Test
    void emptyApprovedSetDeniesEveryoneWhenGateEnabled() {
        assertFalse(AccessDecision.isAllowed(true, Set.of(), NOT_APPROVED));
    }
}
