pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../common" as TX

ColumnLayout {
    id: panel
    Material.theme: Material.Dark
    clip: true
    spacing: 12

    Label { text: "Transmission setup"; font.pixelSize: 22; font.bold: true }
    Label {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        text: "Settings update running TX immediately. Sensor assignments are staged until Apply roles / Start TX or Sync + Calibration."
    }
    RowLayout {
        Button {
            text: "Apply roles / Start TX"
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
    Button {
        text: "Sync + Calibration"
        Layout.fillWidth: true
        enabled: !backend.initializing && !backend.txBusy
        onClicked: backend.txController.calibrate()
    }
    RowLayout {
        enabled: !backend.initializing && !backend.txController.state.exclusive
        Label { text: "Beams" }
        SpinBox { objectName: "radarBeamCount"; from: 1; to: 4; value: backend.txController.state.count; onValueModified: backend.txController.setSetting("count", value) }
        Label { text: "Source samples" }
        ComboBox { model: backend.txController.state.sample_counts; currentIndex: model.indexOf(backend.txController.state.samples); onActivated: backend.txController.setSetting("samples", Number(currentText)) }
    }
    RowLayout {
        enabled: !backend.initializing && !backend.txBusy
        Label { text: "TX sampling rate [MSa/s]" }
        ComboBox {
            objectName: "radarTxSampleRate"
            model: [80, 40, 20, 10, 5, 2.5]
            currentIndex: model.indexOf(backend.txSettings.fs_hz / 1000000)
            onActivated: backend.setTxSetting("fs_hz", model[currentIndex] * 1000000)
        }
    }
    Label {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        text: "Source samples use the selected TX rate. Waveforms are Fourier-resampled for the fixed 80 MSa/s hardware replay; the matched-filter reference is resampled to the RX rate."
    }
    CheckBox {
        text: "Use IQ demo waveforms (otherwise radar pulses)"
        checked: backend.txSettings.waveform_editor
        onClicked: backend.setTxSetting("waveform_editor", checked)
    }
    CheckBox {
        text: "Extended power range"
        checked: backend.txController.state.unlocked
        onClicked: backend.txController.setSetting("unlocked", checked)
    }
    Label { text: "Beam 1 supplies the receive matched-filter reference. Amplitude is applied once. Chirp bandwidth, sample count, delay and repetition also control the preview."; wrapMode: Text.WordWrap; Layout.fillWidth: true }
    TX.IQTxBeamEditor {
        id: beamEditor
        controller: backend.txController
        steeringFirst: true
        sampleTiming: true
        Layout.fillWidth: true
    }
    Switch {
        text: "Beam-squint correction"
        checked: backend.txController.state.beams[beamEditor.beamIndex].squint
        onClicked: backend.txController.setBeam(beamEditor.beamIndex, "squint", checked)
    }
    GroupBox {
        title: "Transmit sensors (firmware IDs)"
        Layout.fillWidth: true
        enabled: !backend.initializing && !backend.txBusy
        ColumnLayout {
            anchors.fill: parent
            Repeater {
                model: backend.txBoardNames
                ColumnLayout {
                    id: boardRow
                    required property string modelData
                    Label { text: boardRow.modelData; Layout.preferredWidth: 100 }
                    CheckBox {
                        text: "All"
                        checked: {
                            for (let a = 0; a < 8; ++a)
                                if (backend.txSettings.tx_sensors.indexOf(boardRow.modelData + "." + a) < 0) return false;
                            return true;
                        }
                        onClicked: backend.setTxSensor(boardRow.modelData, -1, checked)
                    }
                    Flow {
                      Layout.fillWidth: true
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
    }
    GridLayout {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignTop
        columns: 2
        enabled: !backend.initializing && !backend.txController.state.exclusive
        columnSpacing: 24
        Repeater {
            model: [
                {key: "frequency_mhz", label: "Center frequency [MHz]", min: 2310, max: 2640, scale: 1000, step: 1},
                {key: "tx_gain_a", label: "RF gain A", min: 0, max: 7},
                {key: "tx_gain_b", label: "RF gain B", min: 0, max: 7},
                {key: "tx_azimuth", label: "TX azimuth [degrees]", min: -90, max: 90, scale: 100},
                {key: "tx_elevation", label: "TX elevation [degrees]", min: -90, max: 90, scale: 100},
                {key: "chirp_frames", label: "Chirp length [TX samples]", min: 1, max: 256},
                {key: "bandwidth_hz", label: "Chirp bandwidth [MHz]", min: 0.001, max: backend.txSettings.fs_hz / 1000000, scale: 1000, unit: 1000000}
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
    Label {
        text: "Starting TX does not recalibrate. Changes to sensor roles, RF settings, replay length or timing settings require Sync + Calibration. Beam directions and waveform edits reuse calibration while replay origins remain valid."
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
    }
    Item { Layout.preferredHeight: 40 }

}
