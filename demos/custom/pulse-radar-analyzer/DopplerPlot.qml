pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: plot
    color: "#101014"
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 20
        Label {
            Layout.alignment: Qt.AlignHCenter
            text: "Range–Doppler · " + (backend.radarLinearScale ? "linear power" : "dBFS") + " · " + backend.radarMinDb + " to " + backend.radarMaxDb + " dBFS"
            font.pixelSize: 20
        }
        Item {
            id: chart
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.leftMargin: 90
            Layout.bottomMargin: 50
            Image {
                id: samples
                objectName: "dopplerTexture"
                width: Math.max(1, backend.angleSize)
                height: Math.max(1, backend.delaySize)
                cache: false
                asynchronous: true
                // Keep the shader's previous texture until the new image is ready.
                retainWhileLoading: true
                source: "image://dopplermap/" + backend.dopplerFrame
            }
            ShaderEffect {
                objectName: "dopplerRectShader"
                onStatusChanged: if (status === ShaderEffect.Error) console.warn("Doppler shader:", log)
                anchors.fill: parent
                property var source: ShaderEffectSource { sourceItem: samples; hideSource: true; smooth: true }
                property real axisSpan: backend.dopplerDataMaximum-backend.dopplerDataMinimum
                property real xLow: (backend.velocityMinimum-backend.dopplerDataMinimum)/axisSpan
                property real xHigh: (backend.velocityMaximum-backend.dopplerDataMinimum)/axisSpan
                vertexShader: "doppler_rect.vert.qsb"
                fragmentShader: "doppler_rect.frag.qsb"
            }
            Repeater {
                model: 5
                Item {
                    required property int index
                    x: index * chart.width/4
                    height: chart.height
                    Rectangle { width: 1; height: parent.height; color: "#30ffffff" }
                    Label {
                        anchors.top: parent.bottom
                        anchors.horizontalCenter: parent.left
                        text: {
                            const span = backend.velocityMaximum-backend.velocityMinimum;
                            const v = backend.velocityMinimum + index*span/4;
                            const digits = Math.min(9, Math.max(0, Math.ceil(-Math.log10(span/4))+1));
                            return span < 1e-6 ? v.toExponential(4) : v.toFixed(digits);
                        }
                    }
                }
            }
            Repeater {
                model: 5
                Item {
                    required property int index
                    y: (1-index/4)*chart.height
                    width: chart.width
                    Rectangle { width: parent.width; height: 1; color: "#30ffffff" }
                    Label {
                        anchors.right: parent.left
                        anchors.rightMargin: 8
                        anchors.verticalCenter: parent.top
                        text: {
                            const sample = backend.delayMin + index*(backend.delayMax-backend.delayMin-1)/4;
                            return isFinite(backend.radarZeroSample) ? ((sample-backend.radarZeroSample)*backend.radarMetersPerSample).toFixed(2) + " m" : sample.toFixed(0) + " samp";
                        }
                    }
                }
            }
        }
        Label { Layout.alignment: Qt.AlignHCenter; text: "Equivalent radial velocity (m/s, positive receding)" }
        Label {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
            color: "#d8a657"
            text: backend.dopplerProcessingInfo
        }
        Label { Layout.fillWidth: true; text: backend.radarStatus; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter }
    }
}
