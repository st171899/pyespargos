import QtQuick.Controls.Material
import QtQuick.Controls
import QtQuick.Layouts
import QtCharts
import QtQuick

import "../demos/common" as Common
import "../addons/iqsampling/demos/common" as IQ

Common.ESPARGOSApplication {
    id: window
    receiverDrawerComponent: Component { IQ.IQRxDrawer { controller: iqcontrol } }
    title: "Azimuth-Delay IQ Demo"
    minimumWidth: 1024
    minimumHeight: 768

    appDrawerComponent: Component {
        Common.AppDrawer {
            id: appDrawer
            title: "Settings"
            endpoint: appconfig

            Label { Layout.columnSpan: 2; text: "Display Settings"; color: "#9fb3c8" }

            Label { text: "Delay Min [samples]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                property string configKey: "delay_min"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                from: 0
                to: 255
                value: 0
            }

            Label { text: "Delay Max [samples]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                property string configKey: "delay_max"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                from: 0
                to: 255
                value: 255
            }

            Label { text: "Fixed offset [samples]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                property string configKey: "delay_offset_samples"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 160
                from: -255
                to: 255
                value: 0
                editable: true
            }

            Label { text: "Complete chunks only"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            Switch {
                property string configKey: "complete_only"
                property string configProp: "checked"
                Component.onCompleted: appDrawer.configManager.register(this)
                onCheckedChanged: appDrawer.configManager.onControlChanged(this)
                checked: false
            }

            Label { Layout.columnSpan: 2; text: "Pluto Transmission"; color: "#9fb3c8" }

            Label { text: "Chirp length [samples]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: chirpFramesControl
                property string configKey: "chirp_frames"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                from: 1
                to: 256
                value: 16
                editable: true
            }

            Label { text: "Bandwidth [Hz]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: bandwidthControl
                property string configKey: "bandwidth_hz"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                from: 1000000
                to: 40000000
                value: 40000000
                stepSize: 1000000
                editable: true
            }

            Label { text: "TX chirps / 256"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: pingsControl
                property string configKey: "pings_per_256"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                from: 1
                to: 32
                value: 2
            }

            Label { text: "TX gain [dB]"; color: "#ffffff"; horizontalAlignment: Text.AlignRight; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            SpinBox {
                id: txGainControl
                property string configKey: "tx_gain_db"
                property string configProp: "value"
                Component.onCompleted: appDrawer.configManager.register(this)
                onValueChanged: appDrawer.configManager.onControlChanged(this)
                from: -90
                to: 0
                value: -20
                editable: true
            }

            Button {
                text: "Start TX"
                onClicked: backend._start_ping_tx(txGainControl.value, chirpFramesControl.value, bandwidthControl.value, pingsControl.value)
            }
            Button {
                text: "Stop TX"
                onClicked: backend._stop_ping_tx()
            }
        }
    }

    Rectangle {
        id: plotArea
        height: Math.min(parent.height * 0.58, parent.width * 0.28)
        width: 2 * height
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: 12
        color: "#11191e"

        ShaderEffect {
            anchors.fill: parent
            Canvas {
                id: plotCanvas
                width: backend.angleSize
                height: backend.delaySize
                property var imageData: undefined
                function createImageData() {
                    imageData = getContext("2d").createImageData(width, height)
                }
                onAvailableChanged: if (available) createImageData()
                onPaint: if (imageData) getContext("2d").drawImage(imageData, 0, 0)
            }
            property variant source: ShaderEffectSource { sourceItem: plotCanvas; hideSource: true; smooth: true }
            vertexShader: "../addons/iqsampling/demos/azimuth-delay/vertex_shader.qsb"
            fragmentShader: "../addons/iqsampling/demos/azimuth-delay/fragment_shader.qsb"
        }
    }

    Canvas {
        id: labels
        anchors.fill: parent
        property real originX: width / 2
        property real originY: plotArea.y + plotArea.height / 2
        property real radius: plotArea.height / 2
        property real targetAngle: backend.musicAzimuth
        property real targetDelay: backend.targetDelaySample
        property real delaySpan: Math.max(1, backend.delayMax - backend.delayMin)

        function drawPolarGrid(context) {
            context.save()
            context.strokeStyle = "rgba(210, 220, 230, 0.35)"
            context.fillStyle = "rgba(230, 240, 250, 0.8)"
            context.lineWidth = 1
            context.font = "11px sans-serif"
            context.textAlign = "left"
            context.textBaseline = "middle"

            for (let ring = 1; ring <= 4; ring++) {
                const ringRadius = radius * ring / 4
                context.beginPath()
                context.arc(originX, originY, ringRadius, 0, 2 * Math.PI)
                context.stroke()
                const delayValue = backend.delayMinUs + (backend.delayMaxUs - backend.delayMinUs) * ring / 4
                context.fillText(delayValue.toFixed(2) + " us", originX + 5, originY - ringRadius)
            }

            for (let angle = -90; angle <= 90; angle += 30) {
                const radians = angle * Math.PI / 180
                context.beginPath()
                context.moveTo(originX, originY)
                context.lineTo(originX + Math.sin(radians) * radius, originY - Math.cos(radians) * radius)
                context.stroke()
                context.fillText(angle + " deg", originX + Math.sin(radians) * (radius + 8), originY - Math.cos(radians) * (radius + 8))
            }
            context.restore()
        }

        function drawTarget(context) {
            if (!isFinite(targetAngle) || !isFinite(targetDelay)) {
                return
            }
            const radians = targetAngle * Math.PI / 180
            const targetRadius = radius * (targetDelay - backend.delayMin) / delaySpan
            const targetX = originX + Math.sin(radians) * targetRadius
            const targetY = originY - Math.cos(radians) * targetRadius
            context.save()
            context.strokeStyle = "#ffec5c"
            context.fillStyle = "#ffec5c"
            context.lineWidth = 2
            context.beginPath()
            context.arc(targetX, targetY, 8, 0, 2 * Math.PI)
            context.globalAlpha = 0.35
            context.fill()
            context.globalAlpha = 1.0
            context.beginPath()
            context.arc(targetX, targetY, 8, 0, 2 * Math.PI)
            context.stroke()
            context.beginPath()
            context.moveTo(targetX - 12, targetY)
            context.lineTo(targetX + 12, targetY)
            context.moveTo(targetX, targetY - 12)
            context.lineTo(targetX, targetY + 12)
            context.stroke()
            context.font = "12px sans-serif"
            context.textAlign = "left"
            context.fillText(targetAngle.toFixed(1) + " deg, " + backend.targetDelayUs.toFixed(2) + " us", targetX + 12, targetY - 10)
            context.restore()
        }

        onPaint: {
            const context = getContext("2d")
            context.clearRect(0, 0, width, height)
            context.font = "14px sans-serif"
            context.fillStyle = "white"
            context.textAlign = "center"
            context.fillText(backend.delayMinUs.toFixed(2) + " .. " + backend.delayMaxUs.toFixed(2) + " us", originX, originY + 24)
            context.fillText("-90 deg", originX - radius, originY + 18)
            context.fillText("0 deg", originX, originY - radius - 8)
            context.fillText("90 deg", originX + radius, originY + 18)
            drawPolarGrid(context)
            drawTarget(context)
            context.textAlign = "left"
            context.fillText("MUSIC: " + (isFinite(backend.musicAzimuth) ? backend.musicAzimuth.toFixed(1) + " deg" : "waiting"), 12, 24)
            context.fillText("Yellow marker: beamspace peak at MUSIC azimuth", 12, 44)
        }

        Connections {
            target: backend
            function onConfigChanged() { labels.requestPaint() }
            function onTargetChanged() { labels.requestPaint() }
        }
    }

    GridLayout {
        id: iqMonitorGrid
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 10
        height: parent.height * 0.29
        columns: 4
        rows: 2
        columnSpacing: 6
        rowSpacing: 6

        Repeater {
            model: backend.sensorCount

            ChartView {
                required property int index
                Layout.fillWidth: true
                Layout.fillHeight: true
                title: "Antenna " + (index + 1)
                titleColor: "#d7dde5"
                titleFont: Qt.font({ pixelSize: 11 })
                legend.visible: false
                antialiasing: true
                backgroundColor: "#101014"
                backgroundRoundness: 0
                dropShadowEnabled: false
                margins { top: 0; bottom: 0; left: 0; right: 0 }

                ValueAxis {
                    id: sampleAxis
                    min: 0
                    max: backend.iqSampleCount - 1
                    labelsVisible: false
                    gridVisible: false
                    lineVisible: false
                }

                ValueAxis {
                    id: adcAxis
                    min: -backend.adcFullScale
                    max: backend.adcFullScale
                    labelsVisible: false
                    lineVisible: false
                    gridLineColor: "#37373b"
                }

                LineSeries {
                    id: iTrace
                    axisX: sampleAxis
                    axisY: adcAxis
                    color: "#2c7fb8"
                    width: 1.25
                }

                LineSeries {
                    id: qTrace
                    axisX: sampleAxis
                    axisY: adcAxis
                    color: "#d95f0e"
                    width: 1.25
                }

                Timer {
                    interval: 50
                    running: true
                    repeat: true
                    onTriggered: backend.updateIqChart(index, iTrace, qTrace)
                }
            }
        }
    }

    Timer {
        interval: 50
        running: !backend.initializing
        repeat: true
        onTriggered: backend.update_data()
    }

    Connections {
        target: backend
        function onDataChanged(imageData) {
            if (plotCanvas.imageData === undefined || imageData.length !== plotCanvas.imageData.data.length) {
                plotCanvas.imageData = plotCanvas.getContext("2d").createImageData(backend.angleSize, backend.delaySize)
            }
            for (let index = 0; index < imageData.length; index++) {
                plotCanvas.imageData.data[index] = imageData[index]
            }
            plotCanvas.requestPaint()
        }
    }
}