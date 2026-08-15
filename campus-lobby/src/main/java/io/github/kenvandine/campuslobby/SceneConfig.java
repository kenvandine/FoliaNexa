package io.github.kenvandine.campuslobby;

import java.util.Map;

/**
 * Parameters controlling the generated campus scene. Mirrors config.yml's
 * {@code scene}/{@code signs} sections one-for-one — CampusLobbyPlugin is
 * responsible for reading the actual config file and building one of
 * these; this class itself has no org.bukkit.* dependency so CampusScene
 * stays plain-Java-testable.
 */
public record SceneConfig(
        int plazaRadius,
        int towerHeight,
        Include include,
        Colors colors,
        Map<String, String> signLabels
) {

    public record Include(
            boolean belltower,
            boolean wolfStatue,
            boolean tunnel,
            boolean unionFacade,
            boolean libraryFacade
    ) {
    }

    public record Colors(
            String primaryRed,
            String white,
            String black,
            String brick,
            String brickTrim,
            String glass,
            String roof
    ) {
    }

    /** Matches the shipped config.yml defaults exactly. */
    public static SceneConfig defaults() {
        return new SceneConfig(
                24,
                34,
                new Include(true, true, true, true, true),
                new Colors(
                        "RED_CONCRETE",
                        "WHITE_CONCRETE",
                        "BLACK_CONCRETE",
                        "BRICKS",
                        "CHISELED_BRICKS",
                        "WHITE_STAINED_GLASS_PANE",
                        "BLACKSTONE"
                ),
                Map.of()
        );
    }
}
