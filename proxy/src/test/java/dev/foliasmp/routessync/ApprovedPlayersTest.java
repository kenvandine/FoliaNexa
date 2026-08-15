package dev.foliasmp.routessync;

import org.junit.jupiter.api.Test;

import java.util.Set;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ApprovedPlayersTest {

    @Test
    void parsesDashlessMojangStyleUuids() {
        String body = "{\"uuids\": [\"069a79f444e94726a5befca90e38aaf9\"]}";
        Set<UUID> uuids = ApprovedPlayers.parse(body);

        assertEquals(1, uuids.size());
        assertTrue(uuids.contains(UUID.fromString("069a79f4-44e9-4726-a5be-fca90e38aaf9")));
    }

    @Test
    void parsesAlreadyDashedUuids() {
        String body = "{\"uuids\": [\"069a79f4-44e9-4726-a5be-fca90e38aaf9\"]}";
        Set<UUID> uuids = ApprovedPlayers.parse(body);
        assertTrue(uuids.contains(UUID.fromString("069a79f4-44e9-4726-a5be-fca90e38aaf9")));
    }

    @Test
    void parsesMultipleUuids() {
        String body = "{\"uuids\": [\"069a79f444e94726a5befca90e38aaf9\", \"853c80ef3c3749fdaa49938b674adae6\"]}";
        assertEquals(2, ApprovedPlayers.parse(body).size());
    }

    @Test
    void emptyListParsesToEmptySet() {
        assertTrue(ApprovedPlayers.parse("{\"uuids\": []}").isEmpty());
    }

    @Test
    void malformedEntryIsSkippedNotThrown() {
        String body = "{\"uuids\": [\"not-a-real-uuid-at-all\"]}";
        assertTrue(ApprovedPlayers.parse(body).isEmpty());
    }
}
