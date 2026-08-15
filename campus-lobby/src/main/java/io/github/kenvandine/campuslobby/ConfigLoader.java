package io.github.kenvandine.campuslobby;

import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.FileConfiguration;

import java.util.LinkedHashMap;
import java.util.Map;

/** Reads config.yml's {@code scene}/{@code signs} sections into a {@link SceneConfig}. */
final class ConfigLoader {

    private ConfigLoader() {
    }

    static SceneConfig load(FileConfiguration config) {
        SceneConfig defaults = SceneConfig.defaults();

        int plazaRadius = config.getInt("scene.plaza-radius", defaults.plazaRadius());
        int towerHeight = config.getInt("scene.tower-height", defaults.towerHeight());

        SceneConfig.Include include = new SceneConfig.Include(
                config.getBoolean("scene.include.belltower", true),
                config.getBoolean("scene.include.wolf-statue", true),
                config.getBoolean("scene.include.tunnel", true),
                config.getBoolean("scene.include.union-facade", true),
                config.getBoolean("scene.include.library-facade", true)
        );

        SceneConfig.Colors colors = new SceneConfig.Colors(
                config.getString("scene.colors.primary-red", defaults.colors().primaryRed()),
                config.getString("scene.colors.white", defaults.colors().white()),
                config.getString("scene.colors.black", defaults.colors().black()),
                config.getString("scene.colors.brick", defaults.colors().brick()),
                config.getString("scene.colors.brick-trim", defaults.colors().brickTrim()),
                config.getString("scene.colors.glass", defaults.colors().glass()),
                config.getString("scene.colors.roof", defaults.colors().roof())
        );

        Map<String, String> signLabels = new LinkedHashMap<>();
        ConfigurationSection signsSection = config.getConfigurationSection("signs");
        if (signsSection != null) {
            for (String key : signsSection.getKeys(false)) {
                signLabels.put(key, config.getString("signs." + key));
            }
        }

        return new SceneConfig(plazaRadius, towerHeight, include, colors, signLabels);
    }
}
