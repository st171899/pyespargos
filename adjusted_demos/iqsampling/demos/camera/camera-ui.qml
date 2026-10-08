pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../../../../demos/common" as Common
import "../../../../demos/camera" as Camera
import "../common" as IQ

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 1100
	minimumHeight: 700
	title: "IQ Camera"

	receiverDrawerComponent: Component {
		IQ.IQRxDrawer { controller: iqcontrol }
	}

	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "IQ Camera"
			endpoint: appconfig
			contentLayout.width: Math.max(0, appDrawer.width - 40)

			Label { Layout.columnSpan: 2; text: "Camera"; color: "#9fb3c8" }
			Label { text: "Flip"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				property string configKey: "camera.flip"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: false
			}

			Label { text: "FOV Azi"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Slider {
				property string configKey: "camera.fov_azimuth"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 10; to: 179; stepSize: 1; value: 72; implicitWidth: 180
				ToolTip.visible: hovered
				ToolTip.text: Math.round(value) + "°"
			}

			Label { text: "FOV Ele"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Slider {
				property string configKey: "camera.fov_elevation"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 10; to: 120; stepSize: 1; value: 41; implicitWidth: 180
				ToolTip.visible: hovered
				ToolTip.text: Math.round(value) + "°"
			}

			Label { Layout.columnSpan: 2; text: "Beamforming"; color: "#9fb3c8" }
			Label { text: "Method"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: beamformerType
				property string configKey: "beamformer.type"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				model: ["FFT", "MUSIC"]
				currentIndex: 0
				implicitWidth: 180
			}

			Label { text: "Sources"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: beamformerType.currentValue === "MUSIC" }
			SpinBox {
				property string configKey: "beamformer.music_sources"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 1; to: 7; value: 1; editable: true; implicitWidth: 110
				visible: beamformerType.currentValue === "MUSIC"
			}

			Label { text: "FFT"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				property string configKey: "beamformer.fft_size"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				model: [256, 512, 1024]
				currentIndex: 2
				implicitWidth: 180
			}

			Label { text: "Bins"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				id: binMode
				property string configKey: "beamformer.bin_mode"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				textRole: "text"; valueRole: "value"; implicitWidth: 180
				model: [
					{ value: "peak", text: "Peak" },
					{ value: "active", text: "Active" },
					{ value: "band", text: "Band" },
					{ value: "all", text: "All" }
				]
				currentValue: "active"
			}

			Label { text: "Reject DC"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: rejectDc
				property string configKey: "beamformer.reject_dc"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
			}

			Label { text: "DC half-width"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: rejectDc.checked }
			SpinBox {
				property string configKey: "beamformer.dc_half_width"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 0; to: 32; value: 2; editable: true; implicitWidth: 110
				visible: rejectDc.checked
			}

			Label { text: "Threshold"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: binMode.currentValue === "active" }
			Slider {
				property string configKey: "beamformer.threshold_db"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 1; to: 30; stepSize: 1; value: 4; implicitWidth: 180
				visible: binMode.currentValue === "active"
				ToolTip.visible: hovered
				ToolTip.text: Math.round(value) + " dB above noise"
			}

			Label { text: "Coherence"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: binMode.currentValue === "active" }
			Slider {
				property string configKey: "beamformer.min_coherence"
				property string configProp: "value"
				property var encode: function(v) { return Number(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 0; to: 0.95; stepSize: 0.05; value: 0.55; implicitWidth: 180
				visible: binMode.currentValue === "active"
				ToolTip.visible: hovered
				ToolTip.text: "At least " + value.toFixed(2)
			}

			Label { text: "Max bins"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: binMode.currentValue === "active" }
			SpinBox {
				property string configKey: "beamformer.max_bins"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 1; to: 512; value: 64; editable: true; implicitWidth: 110
				visible: binMode.currentValue === "active"
			}

			Label { text: "Band low"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: binMode.currentValue === "band" }
			TextField {
				property string configKey: "beamformer.band_low_mhz"
				property string configProp: "text"
				property var encode: function(v) { return parseFloat(v) }
				property var decode: function(v) { return Number(v).toString() }
				Component.onCompleted: appDrawer.configManager.register(this)
				onEditingFinished: appDrawer.configManager.onControlChanged(this)
				text: "2420"; implicitWidth: 120; visible: binMode.currentValue === "band"
			}

			Label { text: "Band high"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true; visible: binMode.currentValue === "band" }
			TextField {
				property string configKey: "beamformer.band_high_mhz"
				property string configProp: "text"
				property var encode: function(v) { return parseFloat(v) }
				property var decode: function(v) { return Number(v).toString() }
				Component.onCompleted: appDrawer.configManager.register(this)
				onEditingFinished: appDrawer.configManager.onControlChanged(this)
				text: "2450"; implicitWidth: 120; visible: binMode.currentValue === "band"
			}

			Label { text: "Integration"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Slider {
				property string configKey: "beamformer.integration_s"
				property string configProp: "value"
				property var encode: function(v) { return Number(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 0; to: 2; stepSize: 0.05; value: 0.35; implicitWidth: 180
				ToolTip.visible: hovered
				ToolTip.text: value.toFixed(2) + " s"
			}

			Label { Layout.columnSpan: 2; text: "View"; color: "#9fb3c8" }
			Label { text: "Waterfall"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				property string configKey: "visualization.waterfall"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
			}

			Label { text: "Space"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				property string configKey: "visualization.space"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				textRole: "text"; valueRole: "value"; implicitWidth: 180
				model: [{ value: "camera", text: "Camera" }, { value: "beamspace", text: "Beamspace" }]
				currentValue: "camera"
			}

			Label { text: "Range"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Slider {
				property string configKey: "visualization.dynamic_range_db"
				property string configProp: "value"
				property var encode: function(v) { return Math.round(v) }
				property var decode: function(v) { return Number(v) }
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 5; to: 40; stepSize: 1; value: 18; implicitWidth: 180
				ToolTip.visible: hovered
				ToolTip.text: Math.round(value) + " dB"
			}

			Label { text: "Azi shift"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Slider {
				property string configKey: "visualization.azimuth_correction"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: -90; to: 90; stepSize: 1; value: 0; implicitWidth: 180
			}

			Label { text: "Ele shift"; color: "white"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Slider {
				property string configKey: "visualization.elevation_correction"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: -90; to: 90; stepSize: 1; value: 0; implicitWidth: 180
			}
		}
	}

	Camera.CameraOverlay {
		anchors.fill: parent
	}

	Rectangle {
		anchors.top: parent.top
		anchors.horizontalCenter: parent.horizontalCenter
		anchors.topMargin: 10
		width: captureStatus.implicitWidth + 24
		height: 30
		color: "#d0101418"
		border.color: overlayModel.captureStatus === "Calibrated" ? "#35c46a" : "#e0a329"
		radius: 5
		Text {
			id: captureStatus
			anchors.centerIn: parent
			text: overlayModel.captureStatus
			color: "white"
			font.bold: true
		}
	}

	IQ.IQWaterfall {
		visible: overlayModel.waterfallVisible
		model: overlayModel
		anchors.right: parent.right
		anchors.bottom: parent.bottom
		anchors.rightMargin: 14
		anchors.bottomMargin: 14
		width: Math.min(500, parent.width * 0.42)
		height: Math.min(230, parent.height * 0.30)
	}
}
