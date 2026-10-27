pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../common" as TX

ScrollView {
    id: panel
    Material.theme: Material.Dark
    clip: true
    contentWidth: availableWidth

    QtObject {
        id: beamController
        readonly property var state: {
            let az = backend.txSettings.tx_azimuth * Math.PI / 180;
            let el = backend.txSettings.tx_elevation * Math.PI / 180;
            return {count: 1, beams: [{u: Math.sin(az) * Math.cos(el), v: Math.sin(el)}]};
        }
        signal changed()
        onStateChanged: changed()
        function steer(index, u, v) { backend.steerTx(u, v); }
    }

    ColumnLayout {
        width: panel.availableWidth
        spacing: 12
        Label { text: "Transmission setup"; font.pixelSize: 22; font.bold: true }
        Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: "Select the sensors that transmit. All other sensors receive. Use the central Sync + Calibration button to calibrate TX and RX with these settings. Start and stop TX independently."
        }
        RowLayout {
            Button {
                text: "Apply and start TX"
                enabled: !backend.initializing && !backend.txBusy
                onClicked: backend.startTx()
            }
            Button {
                text: "Stop TX"
                enabled: !backend.initializing && !backend.txBusy
                onClicked: backend.stopTx()
            }
            BusyIndicator { running: backend.txBusy; visible: running; Layout.preferredWidth: 36; Layout.preferredHeight: 36 }
            Label { text: backend.txActive ? "Transmitting" : "TX inactive" }
        }
        Label {
            objectName: "txStatusLabel"
            text: backend.txStatus
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
        }
        GroupBox {
            title: "Transmit sensors (firmware IDs)"
            Layout.fillWidth: true
            enabled: !backend.initializing && !backend.txBusy
            ColumnLayout {
                anchors.fill: parent
                Repeater {
                    model: backend.txBoardNames
                    RowLayout {
                        id: boardRow
                        required property string modelData
                        Label { text: boardRow.modelData; Layout.preferredWidth: 160 }
                        CheckBox {
                            text: "All"
                            checked: {
                                for (let a = 0; a < 8; ++a)
                                    if (backend.txSettings.tx_sensors.indexOf(boardRow.modelData + "." + a) < 0) return false;
                                return true;
                            }
                            onClicked: backend.setTxSensor(boardRow.modelData, -1, checked)
                        }
                        Repeater {
                            model: 8
                            CheckBox {
                                required property int index
                                objectName: "txSensor_" + boardRow.modelData + "_" + index
                                text: index.toString()
                                checked: backend.txSettings.tx_sensors.indexOf(boardRow.modelData + "." + index) >= 0
                                onClicked: backend.setTxSensor(boardRow.modelData, index, checked)
                            }
                        }
                    }
                }
            }
        }
        GridLayout {
            Layout.fillWidth: true
            columns: panel.availableWidth < 960 ? 1 : 2
            columnSpacing: 32
        GridLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            columns: 2
            enabled: !backend.initializing && !backend.txBusy
            columnSpacing: 24
            Repeater {
                model: [
                    {key: "frequency_mhz", label: "Center frequency [MHz]", min: 2310, max: 2640, scale: 1000, step: 1},
                    {key: "tx_gain_a", label: "RF gain A", min: 0, max: 7},
                    {key: "tx_gain_b", label: "RF gain B", min: 0, max: 7},
                    {key: "tx_azimuth", label: "TX azimuth [degrees]", min: -90, max: 90, scale: 100},
                    {key: "tx_elevation", label: "TX elevation [degrees]", min: -90, max: 90, scale: 100},
                    {key: "chirp_frames", label: "Chirp length [80 MSa/s samples]", min: 1, max: 256},
                    {key: "bandwidth_hz", label: "Chirp bandwidth [MHz]", min: 0.001, max: 80, scale: 1000, unit: 1000000},
                    {key: "tx_gain_db", label: "Digital waveform gain [dB]", min: -120, max: 0, scale: 100}
                ]
                RowLayout {
                    id: numberRow
                    required property var modelData
                    Layout.columnSpan: 2
                    Label { text: numberRow.modelData.label; Layout.preferredWidth: 270 }
                    SpinBox {
                        property real scale: numberRow.modelData.scale || 1
                        property real unit: numberRow.modelData.unit || 1
                        from: numberRow.modelData.min * scale
                        to: numberRow.modelData.max * scale
                        value: Math.round(backend.txSettings[numberRow.modelData.key] / unit * scale)
                        stepSize: numberRow.modelData.step || 1
                        editable: true
                        textFromValue: function(v, locale) { return Number(v / scale).toLocaleString(locale, "f", Math.round(Math.log10(scale))) }
                        valueFromText: function(t, locale) { return Math.round(Number.fromLocaleString(locale, t) * scale) }
                        onValueModified: backend.setTxSetting(numberRow.modelData.key, value / scale * unit)
                    }
                }
            }
            Repeater {
                model: [
                    {key: "tx_bandwidth", label: "RF bandwidth [MHz]", values: backend.txRFBandwidths},
                    {key: "tx_output", label: "TX output", values: ["antenna_r", "antenna_l", "reference", "isolated"]},
                    {key: "pings_per_256", label: "Chirps per 256 samples", values: [1, 2, 4, 8, 16, 32, 64, 128, 256]},
                    {key: "tx_calibration", label: "TX calibration", values: ["reciprocity", "monitor"]},
                    {key: "calibration_path", label: "Array calibration path", values: ["reference", "antenna_l", "antenna_r"]},
                    {key: "timing_path", label: "Timing calibration path", values: ["reference", "antenna_r", "antenna_l"]}
                ]
                RowLayout {
                    id: choiceRow
                    required property var modelData
                    Layout.columnSpan: 2
                    Label { text: choiceRow.modelData.label; Layout.preferredWidth: 270 }
                    ComboBox {
                        model: choiceRow.modelData.values
                        currentIndex: choiceRow.modelData.values.indexOf(backend.txSettings[choiceRow.modelData.key])
                        enabled: choiceRow.modelData.key !== "calibration_path" || backend.txSettings.tx_calibration !== "monitor"
                        onActivated: backend.setTxSetting(choiceRow.modelData.key, choiceRow.modelData.values[currentIndex])
                    }
                }
            }
            Label {
                Layout.columnSpan: 2
                Layout.fillWidth: true
                text: "Reference timing uses the configured cable delays, with automatic gain recovery for weak splitter coupling. Antenna timing is optional and neglects direct-path travel time."
                wrapMode: Text.WordWrap
                font.pixelSize: 12
            }
            Label { text: "Frequency-dependent steering (squint)" }
            Switch {
                checked: backend.txSettings.tx_squint
                onClicked: backend.setTxSetting("tx_squint", checked)
            }
        }
        GroupBox {
            title: "Transmit beamspace — drag to steer"
            Layout.fillWidth: true
            Layout.preferredWidth: 420
            Layout.maximumWidth: 600
            Layout.alignment: Qt.AlignTop
            enabled: !backend.initializing && !backend.txBusy
            ColumnLayout {
                anchors.fill: parent
                TX.IQTxBeamspace {
                    controller: beamController
                    beamIndex: 0
                    Layout.fillWidth: true
                    Layout.preferredHeight: 400
                }
                Label {
                    text: "Edits take effect with Apply and start TX."
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                }
            }
        }
        }
        Label {
            text: "Settings take effect with Apply and start TX or Sync + Calibration. Starting TX does not recalibrate. Sync measures array corrections, then zero-range timing through the selected timing path. Changes to sensor roles, RF settings, or timing settings require Sync + Calibration."
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
        }
        Item { Layout.preferredHeight: 40 }
    }
}
