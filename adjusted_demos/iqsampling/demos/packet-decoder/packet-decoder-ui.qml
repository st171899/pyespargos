pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtCharts
import "../../../../demos/common" as Common
import "../common" as IQ

Common.ESPARGOSApplication {
    id: window
    visible: true
    width: 1500
    height: 900
    minimumWidth: 1180
    minimumHeight: 700
    title: "IQ Packet Decoder"

    receiverDrawerComponent: Component {
        IQ.IQRxDrawer { controller: iqcontrol }
    }

    appDrawerComponent: Component {
        Common.AppDrawer {
            id: decoderDrawer
            title: "Decoder"
            endpoint: appconfig
            contentLayout.width: Math.max(0, decoderDrawer.width - 40)

            Label { text: "Decode sensors"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                property string configKey: "decode_sensors"
                property string configProp: "value"
                Component.onCompleted: decoderDrawer.configManager.register(this)
                onValueModified: decoderDrawer.configManager.onControlChanged(this)
                from: 1
                to: 8
                value: 1
                editable: true
                ToolTip.visible: hovered
                ToolTip.text: "Try this many antennas, strongest first, for authoritative protocol decoding. Burst classification always uses the strongest antenna."
            }

            Label { text: "Show unknown"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            Switch {
                property string configKey: "show_unknown"
                property string configProp: "checked"
                Component.onCompleted: decoderDrawer.configManager.register(this)
                onCheckedChanged: decoderDrawer.configManager.onControlChanged(this)
                checked: true
                ToolTip.visible: hovered
                ToolTip.text: "Keep unresolved trigger events in the packet list. Counts always include them."
            }

            Item { Layout.columnSpan: 2; Layout.fillWidth: true; implicitHeight: 8 }
            Button {
                Layout.columnSpan: 2
                Layout.alignment: Qt.AlignHCenter
                text: "Clear packet list"
                onClicked: observationModel.clear()
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 58
            color: "#182027"

            ColumnLayout {
                anchors.fill: parent
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                spacing: 1
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Packet detection and protocol decoding"; font.bold: true; font.pixelSize: 15 }
                    Label {
                        text: backend.status
                        color: backend.status.startsWith("Error") ? "#ff6b6b" : "#8bd3ff"
                        Layout.leftMargin: 18
                    }
                    Item { Layout.fillWidth: true }
                    Label { text: backend.stats; color: "#c8ced8" }
                }
                Label {
                    Layout.fillWidth: true
                    text: backend.protocolSummary
                    color: "#92a0b2"
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
            }
        }

        SplitView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Vertical

            Item {
                SplitView.preferredHeight: 390
                SplitView.minimumHeight: 250
                ChartView {
                    id: timeChart
                    anchors.fill: parent
                    anchors.margins: 8
                    title: backend.waveformInfo
                    titleColor: "#d7dce5"
                    legend.alignment: Qt.AlignTop
                    legend.labelColor: "#d7dce5"
                    antialiasing: true
                    backgroundColor: "#101318"
                    plotAreaColor: "#101318"
                    dropShadowEnabled: false

                    ValueAxis {
                        id: timeAxis
                        titleText: "Time [µs]"
                        min: 0; max: 1
                        labelsColor: "#aab3c0"
                        gridLineColor: "#303640"
                    }
                    ValueAxis {
                        id: adcAxis
                        titleText: "ADC counts"
                        min: -512; max: 511
                        labelsColor: "#aab3c0"
                        gridLineColor: "#303640"
                    }
                    LineSeries {
                        id: iTrace
                        name: "I"
                        axisX: timeAxis; axisY: adcAxis
                        color: "#45a8ff"; width: 1.1
                        useOpenGL: Qt.platform.os === "linux"
                    }
                    LineSeries {
                        id: qTrace
                        name: "Q"
                        axisX: timeAxis; axisY: adcAxis
                        color: "#ff8a45"; width: 1.1
                        useOpenGL: Qt.platform.os === "linux"
                    }
                    LineSeries {
                        id: magnitudeTrace
                        name: "|IQ|"
                        axisX: timeAxis; axisY: adcAxis
                        color: "#74d680"; width: 1.1
                        useOpenGL: Qt.platform.os === "linux"
                    }
                    Timer {
                        interval: 50
                        running: true
                        repeat: true
                        onTriggered: backend.updateTimeChart(iTrace, qTrace, magnitudeTrace, timeAxis, adcAxis)
                    }
                }
            }

            Rectangle {
                SplitView.fillHeight: true
                SplitView.minimumHeight: 260
                color: "#0d1015"

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 2

                    Label {
                        text: "Recent packet observations"
                        font.bold: true
                        font.pixelSize: 15
                        Layout.bottomMargin: 5
                    }

                    Row {
                        id: headerRow
                        spacing: 0
                        property var widths: [76, 190, 135, 64, 78, 112, 88, 82, 500, 54]
                        Repeater {
                            model: ["Time", "Protocol / family", "Result", "Conf.", "Duration", "Center", "BW", "Rate", "Details", "Sensor"]
                            Rectangle {
                                id: headerCell
                                required property int index
                                required property string modelData
                                width: headerRow.widths[index]
                                height: 30
                                color: "#252b34"
                                border.color: "#3b424d"
                                Label {
                                    anchors.fill: parent
                                    leftPadding: 6
                                    verticalAlignment: Text.AlignVCenter
                                    text: headerCell.modelData
                                    font.bold: true
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }

                    ListView {
                        id: packets
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: observationModel
                        ScrollBar.vertical: ScrollBar {}
                        ScrollBar.horizontal: ScrollBar {}
                        contentWidth: 1379
                        delegate: Rectangle {
                            id: packetRow
                            required property int index
                            required property string clock
                            required property string protocol
                            required property string status
                            required property string confidence
                            required property string duration
                            required property string frequency
                            required property string bandwidth
                            required property string rate
                            required property string summary
                            required property string sensor
                            width: 1379
                            height: 29
                            color: index % 2 ? "#141920" : "#10151b"
                            property var values: [clock, protocol, status, confidence, duration, frequency, bandwidth, rate, summary, sensor]
                            property var widths: [76, 190, 135, 64, 78, 112, 88, 82, 500, 54]
                            Row {
                                Repeater {
                                    model: 10
                                    Label {
                                        required property int index
                                        width: packetRow.widths[index]
                                        height: 29
                                        leftPadding: 6
                                        verticalAlignment: Text.AlignVCenter
                                        text: packetRow.values[index]
                                        color: packetRow.status.startsWith("decoded") ? "#bdeec3" : "#d5dae2"
                                        font.pixelSize: 12
                                        elide: Text.ElideRight
                                        HoverHandler { id: cellHover }
                                        ToolTip.visible: cellHover.hovered && truncated
                                        ToolTip.text: text
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
