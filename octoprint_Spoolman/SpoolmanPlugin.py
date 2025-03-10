import octoprint.plugin
from octoprint.events import Events
from flask import request, jsonify

from .modules.PluginAPI import PluginAPI
from .modules.PrinterHandler import PrinterHandler
from .modules.PrinterUtils import PrinterUtils
from .modules.SpoolmanConnector import SpoolmanConnector
from .common.settings import SettingsKeys
from .common.events import PluginEvents

class SpoolmanPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.BlueprintPlugin,
    PluginAPI,
    PrinterHandler
):
    _isInitialized = False

    def initialize(self):
        self._isInitialized = True
        self._printer_utils = PrinterUtils(self._printer, self._file_manager)
        PrinterHandler.initialize(self)

    # Using composition for PrinterUtils instead of inheritance
    def getCurrentJobFilamentUsage(self):
        return self._printer_utils.getCurrentJobFilamentUsage()

    # TODO: Investigate caching again in the future.
    # Currently re-instantiating is fine, as there's nothing "heavy" in the ctor,
    # nor there's any useful persistence in the class itself.
    def getSpoolmanConnector(self):
        spoolmanInstanceUrl = self._settings.get([ SettingsKeys.SPOOLMAN_URL ])

        verifyConfig = None

        isSpoolmanCertVerifyEnabled = self._settings.get([ SettingsKeys.IS_SPOOLMAN_CERT_VERIFY_ENABLED ])
        spoolmanCertPemPath = self._settings.get([ SettingsKeys.SPOOLMAN_CERT_PEM_PATH ])

        if isSpoolmanCertVerifyEnabled:
            if spoolmanCertPemPath:
                verifyConfig = spoolmanCertPemPath
            else:
                verifyConfig = True
        else:
            verifyConfig = False

        return SpoolmanConnector(
            instanceUrl = spoolmanInstanceUrl,
            logger = self._logger,
            verifyConfig = verifyConfig,
        )

    def triggerPluginEvent(self, eventType, eventPayload = {}):
        # Füge das "plugin_Spoolman_" Präfix hinzu, wenn es noch nicht vorhanden ist
        if not eventType.startswith("plugin_"):
            eventType = f"plugin_Spoolman_{eventType}"
            
        self._logger.info("[Spoolman][event] Triggered '" + eventType + "' with payload '" + str(eventPayload) + "'")
        self._event_bus.fire(
            eventType,
            payload = eventPayload
        )

    def on_after_startup(self):
        self._logger.info("[Spoolman][init] Plugin activated")

    # Printing events handlers
    def on_event(self, event, payload):
        if (
            event == Events.PRINT_STARTED or
            event == Events.PRINT_PAUSED or
            event == Events.PRINT_DONE or
            event == Events.PRINT_FAILED or
            event == Events.PRINT_CANCELLED
        ):
            self.handlePrintingStatusChange(event)

        pass

    def on_sentGCodeHook(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if not self._isInitialized:
            return

        self.handlePrintingGCode(cmd)

        pass

    # --- Mixins ---

    # AssetPlugin
    def get_assets(self):
        return {
            "js": [
                "js/common/api.js",
                "js/common/promiseCache.js",
                "js/common/octoprint.js",
                "js/common/formatting.js",
                "js/api/getSpoolmanSpools.js",
                "js/api/getCurrentJobRequirements.js",
                "js/api/updateActiveSpool.js",
                "js/Spoolman_api.js",
                "js/Spoolman_sidebar.js",
                "js/Spoolman_settings.js",
                "js/Spoolman_modal_selectSpool.js",
                "js/Spoolman_modal_confirmSpool.js",
            ],
            "css": [
                "css/Spoolman.css",
            ],
            "less": [],
        }

    # TemplatePlugin
    def get_template_configs(self):
        return [
            {
                "type": "sidebar",
                "template": "Spoolman_sidebar.jinja2",
            },
            {
                "type": "settings",
                "template": "Spoolman_settings.jinja2",
            }
        ]

    # SettingsPlugin
    def get_settings_defaults(self):
        settings = {
            SettingsKeys.INSTALLED_VERSION: self._plugin_version,
            SettingsKeys.SPOOLMAN_URL: "",
            SettingsKeys.IS_SPOOLMAN_CERT_VERIFY_ENABLED: True,
            SettingsKeys.SPOOLMAN_CERT_PEM_PATH: "",
            SettingsKeys.SELECTED_SPOOL_IDS: {},
            SettingsKeys.IS_PREPRINT_SPOOL_VERIFY_ENABLED: True,
            SettingsKeys.SHOW_LOT_NUMBER_COLUMN_IN_SPOOL_SELECT_MODAL: False,
            SettingsKeys.SHOW_LOT_NUMBER_IN_SIDE_BAR: False,
            SettingsKeys.SHOW_SPOOL_ID_IN_SIDE_BAR: False,
        }

        return settings

    def on_settings_save(self, data):
        self._logger.info("[Spoolman][Settings] Saved data")

        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

    # Event bus
    def register_custom_events(*args, **kwargs):
        return [
            PluginEvents.SPOOL_SELECTED,
            PluginEvents.SPOOL_USAGE_COMMITTED,
            PluginEvents.SPOOL_USAGE_ERROR,
        ]

    def get_update_information(self):
        return {
            "Spoolman": {
                "displayName": "Spoolman Plugin",
                "displayVersion": self._plugin_version,

                # version check: github repository
                "type": "github_release",
                "user": "mdziekon",
                "repo": "octoprint-spoolman",
                "current": self._plugin_version,

                # update method: pip
                "pip": "https://github.com/mdziekon/octoprint-spoolman/archive/{target_version}.zip",

                "stable_branch": {
                    "name": "Stable",
                    "branch": "stable",
                    "comittish": ["stable"],
                },
                "prerelease_branches": [
                    {
                        "name": "Release candidate",
                        "branch": "rc",
                        "comittish": ["rc", "stable"],
                    },
                ]
            }
        }

    # Webhook for FilaMan to select a spool
    @octoprint.plugin.BlueprintPlugin.route("/selectSpool", methods=["POST"])
    def select_spool_webhook(self):
        if not self._isInitialized:
            return jsonify({"error": "Plugin not initialized"}), 503
            
        try:
            data = request.json
            
            if not data:
                return jsonify({"error": "Missing request data"}), 400
                
            spool_id = data.get("spool_id")
            if spool_id is None:
                return jsonify({"error": "Missing spool_id parameter"}), 400
                
            # Optional Parameter
            tool = data.get("tool", "tool0")
            
            # Extrahiere nur die Nummer aus dem Tool-Namen
            tool_index = tool.replace("tool", "") if tool.startswith("tool") else tool
            
            # Bestehende Settings holen
            selected_spool_ids = self._settings.get([SettingsKeys.SELECTED_SPOOL_IDS])
            
            # Spule für das angegebene Tool auswählen
            selected_spool_ids[tool_index] = {
                'spoolId': str(spool_id)
            }
            
            # Settings aktualisieren
            self._settings.set([SettingsKeys.SELECTED_SPOOL_IDS], selected_spool_ids)
            self._settings.save()
            
            # Event auslösen
            self.triggerPluginEvent(
                PluginEvents.SPOOL_SELECTED,
                {
                    'toolIdx': int(tool_index) if tool_index.isdigit() else tool_index,
                    'spoolId': str(spool_id)
                }
            )
            
            return jsonify({
                "success": True,
                "selected_spool": spool_id,
                "tool": tool_index
            }), 200
            
        except Exception as e:
            self._logger.error(f"[Spoolman][API] Error in webhook: {str(e)}")
            return jsonify({"error": f"Internal error: {str(e)}"}), 500
