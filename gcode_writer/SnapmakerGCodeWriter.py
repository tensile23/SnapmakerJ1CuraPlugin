import base64
from typing import List

from PyQt6.QtCore import QBuffer
from UM.Application import Application
from UM.FileHandler.FileWriter import FileWriter
from UM.Logger import Logger
from UM.Math.AxisAlignedBox import AxisAlignedBox
from UM.Math.Vector import Vector
from UM.Mesh.MeshWriter import MeshWriter
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
from UM.i18n import i18nCatalog

from cura.CuraApplication import CuraApplication
from cura.Settings.ExtruderManager import ExtruderManager
from cura.Snapshot import Snapshot
from cura.Utils.Threading import call_on_qt_thread
from ..config import SNAPMAKER_DISCOVER_MACHINES

catalog = i18nCatalog("cura")


class GCodeInfo:

    def __init__(self) -> None:
        self.bbox = AxisAlignedBox()
        self.flavor = 'Marlin'
        self.line_count = 0


class SnapmakerGCodeWriter(MeshWriter):
    """GCode Writer that writes G-code in Snapmaker favour.

    - Add Snapmaker specific headers and thumbnail
    """

    def __init__(self) -> None:
        super().__init__(add_to_recent_files=True)

        self._extruder_mode = "Normal"
        self._header_version = 1

    def setExtruderMode(self, extruder_mode: str) -> None:
        self._extruder_mode = extruder_mode

    def __detectHeaderVersion(self):
        global_stack = CuraApplication.getInstance().getGlobalContainerStack()
        machine_name = global_stack.getProperty("machine_name", "value")

        for machine in SNAPMAKER_DISCOVER_MACHINES:
            if machine['name'] == machine_name:
                break

        self._header_version = machine.get('header_version', 1) if machine else -1

    def write(self, stream, node, mode=FileWriter.OutputMode.BinaryMode) -> None:
        """Writes the G-code for the entire scene to a stream.

        Copied from GCodeWriter, do little modifications.
        """

        if mode != MeshWriter.OutputMode.TextMode:
            Logger.log("e", "GCodeWriter does not support non-text mode.")
            self.setInformation(catalog.i18nc("@error:not supported", "GCodeWriter does not support non-text mode."))
            return False

        active_build_plate = Application.getInstance().getMultiBuildPlateModel().activeBuildPlate
        scene = Application.getInstance().getController().getScene()
        if not hasattr(scene, "gcode_dict"):
            self.setInformation(catalog.i18nc("@warning:status", "Please prepare G-code before exporting."))
            return False

        gcode_dict = getattr(scene, "gcode_dict")
        gcode_list = gcode_dict.get(active_build_plate, None)
        if gcode_list is not None:
            self.processGCodeList(stream, gcode_list)
            return True

        self.setInformation(catalog.i18nc("@warning:status", "Please prepare G-code before exporting."))
        return False

    @call_on_qt_thread
    def __generateThumbnail(self) -> str:
        """Generate thumbnail using PreviewPass.

        Need to be run on Qt thread.
        """
        try:
            image = Snapshot.snapshot(600, 600)
            if not image:
                return ""

            buffer = QBuffer()
            buffer.open(QBuffer.OpenModeFlag.ReadWrite)
            image.save(buffer, "PNG")
            base64_bytes = base64.b64encode(buffer.data())
            base64_message = base64_bytes.decode("ascii")
            buffer.close()

            return "data:image/png;base64," + base64_message
        except Exception:
            Logger.logException("w", "Failed to create thumbnail for G-code")
            return ""

    def __parseOriginalGCode(self, gcode_list: List[str]) -> GCodeInfo:
        """Parse Original GCode to get info.

        ;FLAVOR:Marlin\n;TIME:6183\n;Filament used: 3.21557m, 0m\n;Layer height: 0.1\n;MINX:136.734\n;MINY:74.638\n;MINZ:0.3\n;MAXX:186.578\n;MAXY:125.365\n;MAXZ:52\n
        """

        check_header_line = True
        line_count = 0

        key_value_pairs = {}

        for gcode in gcode_list:
            lines = gcode.split('\n')
            line_count += len(lines) - 1

            if check_header_line:
                for line in lines:
                    if line.startswith(";Generated with Cura_SteamEngine"):  # header ends
                        check_header_line = False
                        break

                    if line.startswith(";") and ':' in line:
                        line = line[1:].strip()
                        key, value = line.split(":", 1)
                        value = value.strip()

                        key_value_pairs[key] = value

        gcode_info = GCodeInfo()
        if "FLAVOR" in key_value_pairs:
            gcode_info.flavour = key_value_pairs["FLAVOR"]
        if "MINX" in key_value_pairs:
            gcode_info.bbox = AxisAlignedBox(
                Vector(float(key_value_pairs["MINX"]), float(key_value_pairs["MINY"]), float(key_value_pairs["MINZ"])),
                Vector(float(key_value_pairs["MAXX"]), float(key_value_pairs["MAXY"]), float(key_value_pairs["MAXZ"])),
            )
        gcode_info.line_count = line_count

        return gcode_info

    def processGCodeList(self, stream, gcode_list: List[str]) -> None:
        self.__detectHeaderVersion()

        if self._header_version == 1:
            self._processGCodeListV1(stream, gcode_list)
        elif self._header_version == 0:
            self._processGCodeListLegacy(stream, gcode_list)
        else:
            # Unsupported machine header, just use original
            self._processGCodeListTransparent(stream, gcode_list)

    def _processGCodeListV1(self, stream, gcode_list: List[str]) -> None:
        try:
            gcode_info = self.__parseOriginalGCode(gcode_list)
        except KeyError:
            gcode_info = None

        print_info = CuraApplication.getInstance().getPrintInformation()
        global_stack = CuraApplication.getInstance().getGlobalContainerStack()
        scene = CuraApplication.getInstance().getController().getScene()

        machine_name = global_stack.getProperty("machine_name", "value")

        # convert Duration to int
        estimated_time = int(print_info.currentPrintTime)

        headers = [
            ";Header Start",
            ";Version:1",
            ";Slicer:CuraEngine",
            ";Printer:{}".format(machine_name),
            ";Estimated Print Time:{}".format(estimated_time),
            ";Lines:{}".format(gcode_info.line_count if gcode_info else 0),
            ";Extruder Mode:{}".format(self._extruder_mode),
        ]

        for extruder in global_stack.extruderList:
            nozzle_size = extruder.getProperty("machine_nozzle_size", "value")

            material = extruder.material
            temperature = extruder.getProperty("material_print_temperature", "value")

            retraction_amount = extruder.getProperty("retraction_amount", "value")
            switch_retraction_amount = extruder.getProperty("switch_extruder_retraction_amount", "value")

            headers.append(";Extruder {} Nozzle Size:{}".format(extruder.position, nozzle_size))
            headers.append(";Extruder {} Material:{}".format(extruder.position, material.getName()))
            headers.append(";Extruder {} Print Temperature:{}".format(extruder.position, temperature))
            headers.append(";Extruder {} Retraction Distance:{}".format(extruder.position, retraction_amount))
            headers.append(";Extruder {} Switch Retraction Distance:{}".format(extruder.position, switch_retraction_amount))

        bed_temperature = global_stack.getProperty("material_bed_temperature_layer_0", "value")
        headers.append(";Bed Temperature:{}".format(bed_temperature))

        extruders_used = set()
        for node in DepthFirstIterator(scene.getRoot()):
            stack = node.callDecoration("getStack")
            if not stack:
                continue

            extruder_nr = stack.getProperty("extruder_nr", "value")
            extruders_used.add(int(extruder_nr))

        headers.append(";Extruder(s) Used:{}".format(len(extruders_used)))

        if gcode_info and gcode_info.bbox.isValid():
            bbox = gcode_info.bbox
            headers.extend([
                ";Work Range - Min X:{}".format(bbox.minimum.x),
                ";Work Range - Min Y:{}".format(bbox.minimum.y),
                ";Work Range - Min Z:{}".format(bbox.minimum.z),
                ";Work Range - Max X:{}".format(bbox.maximum.x),
                ";Work Range - Max Y:{}".format(bbox.maximum.y),
                ";Work Range - Max Z:{}".format(bbox.maximum.z),
            ])

        thumbnail = self.__generateThumbnail()
        headers.append(";Thumbnail:{}".format(thumbnail))

        headers.append(";Header End")
        headers.append("")

        stream.write("\n".join(headers))

        for gcode in gcode_list:
            stream.write(gcode)

    def __getExtruderValue(self, key) -> str:
        extruder_stack = ExtruderManager.getInstance().getActiveExtruderStack()

        type_ = extruder_stack.getProperty(key, "type")
        value_ = extruder_stack.getProperty(key, "value")

        if str(type_) == "float":
            value = "{:.4f}".format(value_).rstrip("0").rstrip(".")
        else:
            if str(type_) == "enum":
                options_ = extruder_stack.getProperty(key, "options")
                value = options_[str(value_)]
            else:
                value = str(value_)

        return value

    def _processGCodeListLegacy(self, stream, gcode_list: List[str]) -> None:
        try:
            gcode_info = self.__parseOriginalGCode(gcode_list)
        except KeyError:
            gcode_info = None

        print_info = CuraApplication.getInstance().getPrintInformation()
        global_stack = CuraApplication.getInstance().getGlobalContainerStack()

        machine_name = global_stack.getProperty("machine_name", "value")

        # convert Duration to int
        estimated_time = int(print_info.currentPrintTime)

        print_temp = float(self.__getExtruderValue("material_print_temperature"))
        bed_temp = float(self.__getExtruderValue("material_bed_temperature")) or 0.
        print_speed = float(self.__getExtruderValue("speed_infill"))

        headers = [
            ";Header Start",

            # legacy keys
            ";header_type: 3dp",
            ";file_total_lines: {}".format(gcode_info.line_count if gcode_info else 0),
            ";estimated_time(s): {:.02f}".format(estimated_time),
            ";nozzle_temperature(°C): {:.0f}".format(print_temp),
            ";build_plate_temperature(°C): {:.0f}".format(bed_temp),
            ";work_speed(mm/minute): {:.0f}".format(print_speed),

            # keys for Version 1
            # ";Version:0",
            # ";Slicer:CuraEngine",
            ";Printer:{}".format(machine_name),
            # ";Estimated Print Time:{}".format(estimated_time),
            # ";Lines:{}".format(gcode_info.line_count if gcode_info else 0),
            # ";Extruder Mode:{}".format(self._extruder_mode),
        ]

        for extruder in global_stack.extruderList:
            nozzle_size = extruder.getProperty("machine_nozzle_size", "value")

            material = extruder.material
            temperature = extruder.getProperty("material_print_temperature", "value")

            retraction_amount = extruder.getProperty("retraction_amount", "value")
            switch_retraction_amount = extruder.getProperty("switch_extruder_retraction_amount", "value")

            headers.append(";Extruder {} Nozzle Size:{}".format(extruder.position, nozzle_size))
            headers.append(";Extruder {} Material:{}".format(extruder.position, material.getName()))
            headers.append(";Extruder {} Print Temperature:{}".format(extruder.position, temperature))
            headers.append(";Extruder {} Retraction Distance:{}".format(extruder.position, retraction_amount))
            headers.append(";Extruder {} Switch Retraction Distance:{}".format(extruder.position, switch_retraction_amount))

        if gcode_info and gcode_info.bbox.isValid():
            bbox = gcode_info.bbox
            headers.extend([
                ";min_x(mm): {}".format(bbox.minimum.x),
                ";min_y(mm): {}".format(bbox.minimum.y),
                ";min_z(mm): {}".format(bbox.minimum.z),
                ";max_x(mm): {}".format(bbox.maximum.x),
                ";max_y(mm): {}".format(bbox.maximum.y),
                ";max_z(mm): {}".format(bbox.maximum.z),
            ])

        thumbnail = self.__generateThumbnail()
        headers.append(";thumbnail: {}".format(thumbnail))

        headers.append(";Header End")
        headers.append("")

        stream.write("\n".join(headers))

        for gcode in gcode_list:
            stream.write(gcode)

    def _processGCodeListTransparent(self, stream, gcode_list: List[str]) -> None:
        for gcode in gcode_list:
            stream.write(gcode)
