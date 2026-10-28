pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtCharts
import "../common" as Common
import "../common" as IQ

Common.ESPARGOSApplication {
	id: window
	visible: true
	minimumWidth: 1280
	minimumHeight: 800

	title: "Pulse Radar Analyzer"

	// Universal antenna colour coding (same palette/order as the web interface)
	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]
	property var antidToColorIndex: [4, 5, 6, 7, 0, 1, 2, 3]
	function antennaColor(antid) { return colorCycle[antidToColorIndex[antid]] }
	function formatDbfs(value) { return isFinite(value) ? value.toFixed(1) + " dBFS" : "−∞ dBFS" }

	// Front view of the array: top row antids 4..7, bottom row 0..3
	property var frontViewRows: [[4, 5, 6, 7], [0, 1, 2, 3]]

    component RadarRangeField: TextField {
        id: rangeField
        required property bool upperBound
        readonly property real sample: upperBound ? backend.delayMax : backend.delayMin
        readonly property real meters: (sample - backend.radarZeroSample) * backend.radarMetersPerSample
        enabled: isFinite(backend.radarZeroSample)
        placeholderText: "Calibrate range first"
        Layout.fillWidth: true
        selectByMouse: true
        validator: DoubleValidator { locale: "C"; notation: DoubleValidator.StandardNotation }
        function refresh() {
            text = isFinite(meters) ? meters.toFixed(3) : "";
        }
        onMetersChanged: if (!activeFocus) refresh()
        Component.onCompleted: refresh()
        onEditingFinished: {
            if (acceptableInput && text.trim().length > 0)
                backend.setRadarRangeMeters(Number(text), upperBound);
            refresh();
        }
        ToolTip.visible: hovered
        ToolTip.text: "Enter meters. Snaps to the nearest received sample, keeping at least one sample in the observation interval."
    }

	/** RX drawer: the reusable IQ capture settings (replaces the CSI pool drawer) **/
	receiverDrawerComponent: Component {
		IQ.IQRxDrawer {
			controller: iqcontrol
            enabled: !backend.txBusy
		}
	}

	/** App drawer: app-specific display settings only **/
	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Radar settings"
            implicitWidth: 600
            objectName: "radarAppDrawer"
			endpoint: appconfig
			// Keep this addon's grid narrower than the drawer viewport. AppDrawer
			// then has no horizontal overflow to scroll, without changing the
			// shared component used by WiFi demos.
			contentLayout.width: Math.max(0, appDrawer.width - 40)

            TabBar {
                id: settingsTabs
                objectName: "radarSettingsTabs"
                Layout.columnSpan: 2
                Layout.fillWidth: true
                TabButton { text: "Display" }
                TabButton { text: "Transmitter" }
            }
            GridLayout {
                columns: 2
                Layout.columnSpan: 2
                Layout.fillWidth: true
                visible: settingsTabs.currentIndex === 0

			Label { text: "Mode"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				property string configKey: "display_mode"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180
				textRole: "text"
				valueRole: "value"
				model: [
					{ value: "power", text: "Power waterfall" },
					{ value: "phase", text: "Relative phase" },
					{ value: "time", text: "Time domain I/Q" },
					{ value: "ping", text: "Ping TX / Time IQ" },
					{ value: "constellation", text: "I/Q constellation" },
					{ value: "spectrum", text: "Amplitude spectrum" },
					{ value: "angle", text: "Ping angle of arrival" },
					{ value: "radar", text: "2D Range-Azimuth Radar Map" }
				]
				currentValue: "power"
			}

			Label { text: "FFT"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			ComboBox {
				property string configKey: "fft_size"
				property string configProp: "currentValue"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
				implicitWidth: 180
				model: [256, 512, 1024]
				currentIndex: 2
				ToolTip.visible: hovered
				ToolTip.text: "Samples per display update in every mode: one waterfall row, chart trace or constellation spans FFT/256 consecutive chunks, so the trigger must deliver contiguous runs of at least that many chunks (e.g. interval burst >= 4 for 1024)."
			}

			Label { text: "Use Calibration"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				property string configKey: "apply_calibration"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: true
				ToolTip.visible: hovered
				ToolTip.text: "Apply the fine per-sensor time/phase calibration to the relative-phase display. No effect until a calibration has been run (drawer button or startup)."
			}

			Label { text: "Complete only"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				property string configKey: "complete_only"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: false
				ToolTip.visible: hovered
				ToolTip.text: "On: render only sets where ALL sensors delivered the chunk (incomplete data is skipped). Off: render everything; sensors missing a chunk show black / placeholder rows."
			}


			Label { text: "Clutter removal"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				id: removeLosSwitch
                objectName: "clutterRemovalSwitch"
				property string configKey: "remove_los"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: false
				ToolTip.visible: hovered
				ToolTip.text: "Subtract static background in Ping, Angle, and Radar modes. Manual: capture it with the button. Automatic: continuously learn recent acquisitions to emphasize moving targets."
			}

            Label { text: "Clutter mode"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
            ComboBox {
                id: clutterModeSelector
                objectName: "clutterModeSelector"
                property string configKey: "clutter_mode"
                property string configProp: "currentValue"
                Component.onCompleted: appDrawer.configManager.register(this)
                onCurrentValueChanged: appDrawer.configManager.onControlChanged(this)
                implicitWidth: 180
                textRole: "text"
                valueRole: "value"
                model: [
                    {value: "manual", text: "Manual (200 chunks)"},
                    {value: "automatic", text: "Automatic (rolling)"}
                ]
                currentValue: "manual"
                ToolTip.visible: hovered
                ToolTip.text: "Automatic subtracts the previous 16 raw-IQ chunks, retaining at most 0.4 seconds of history. Stationary targets fade; slow motion may also be attenuated."
            }

			// Empty label slot to align the action button onto the right side column layout
			Item {
				Layout.fillWidth: true
				height: 1
			}
			Button {
				text: "Calibrate Clutter"
                objectName: "calibrateClutterButton"
                enabled: backend.clutterMode === "manual"
				Layout.preferredWidth: 180
				onClicked: backend.calibrateClutter()
				ToolTip.visible: hovered
				ToolTip.text: "With the scene clear of moving targets and receiver settings fixed, average 200 synchronized 256-sample received-IQ chunks. The captured background stays fixed until you press this button again."
			}

			Label {
				Layout.columnSpan: 2
				Layout.fillWidth: true
				text: backend.clutterStatusText
				color: (backend.clutterStatusText === "Clutter removal active" || (backend.clutterMode === "automatic" && removeLosSwitch.checked)) ? "#9fd18b" : (backend.clutterStatusText === "Calibrating clutter..." ? "#f2c14e" : "#9aa4b2")
				font.pixelSize: 12
				font.bold: true
				font.family: "monospace"
				horizontalAlignment: Text.AlignLeft
				Layout.topMargin: 4
				Layout.bottomMargin: 8
			}



            Label { text: "Range start [m]"; Layout.alignment: Qt.AlignRight }
            RadarRangeField { objectName: "radarMinRangeField"; upperBound: false }
            Label { text: "Range end [m]"; Layout.alignment: Qt.AlignRight }
            RadarRangeField { objectName: "radarMaxRangeField"; upperBound: true }
            Label {
                Layout.columnSpan: 2
                Layout.fillWidth: true
                text: isFinite(backend.radarZeroSample)
                    ? "Resolution: " + backend.radarMetersPerSample.toFixed(3) + " m/sample · applied samples " + backend.delayMin + "–" + backend.delayMax
                    : "Use Sync + Calibration to enter observation limits in meters."
                color: "#9aa4b2"
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }

            Label { text: "Radar color scale"; Layout.alignment: Qt.AlignRight }
            ComboBox {
                objectName: "radarScaleSelector"
                property string configKey: "radar_scale"
                property string configProp: "currentIndex"
                function encode(index) { return index === 1 ? "linear" : "db"; }
                function decode(value) { return value === "linear" ? 1 : 0; }
                model: ["dB", "Linear power"]
                Component.onCompleted: appDrawer.configManager.register(this)
                onCurrentIndexChanged: appDrawer.configManager.onControlChanged(this)
            }

            Label { text: "Color limits [dBFS]"; Layout.alignment: Qt.AlignRight }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                RangeSlider {
                    id: radarPowerLimits
                    objectName: "radarPowerRangeSlider"
                    property alias minimumValue: radarPowerLimits.first.value
                    property alias maximumValue: radarPowerLimits.second.value
                    Layout.fillWidth: true
                    from: -120
                    to: 20
                    stepSize: 1
                    snapMode: RangeSlider.SnapAlways
                    first.value: -50
                    second.value: 0
                    first.onValueChanged: {
                        if (first.value >= second.value) first.value = second.value - 1;
                    }
                    second.onValueChanged: {
                        if (second.value <= first.value) second.value = first.value + 1;
                    }
                }
                Label {
                    text: "Min " + radarPowerLimits.first.value.toFixed(0) + "   ·   Max " + radarPowerLimits.second.value.toFixed(0)
                    font.pixelSize: 11
                    Layout.alignment: Qt.AlignHCenter
                }
                QtObject {
                    property string configKey: "radar_min_db"
                    property string configProp: "value"
                    property alias value: radarPowerLimits.minimumValue
                    Component.onCompleted: appDrawer.configManager.register(this)
                    onValueChanged: appDrawer.configManager.onControlChanged(this)
                }
                QtObject {
                    property string configKey: "radar_max_db"
                    property string configProp: "value"
                    property alias value: radarPowerLimits.maximumValue
                    Component.onCompleted: appDrawer.configManager.register(this)
                    onValueChanged: appDrawer.configManager.onControlChanged(this)
                }
            }

            }
            RadarTxSettings {
                visible: settingsTabs.currentIndex === 1
                Layout.columnSpan: 2
                Layout.fillWidth: true
            }



		}
	}

	/** 2x4 antenna displays, front view; one tab per board on multi-board
	    coherent arrays (only the active board is rendered) **/
	ColumnLayout {
		anchors.fill: parent
		anchors.margins: 10
		spacing: 8

		TabBar {
			id: boardTabs
            objectName: "receiveBoardTabs"
            currentIndex: backend.activeBoard
			visible: backend.boardCount > 1
			Layout.fillWidth: true
			z: 20
			onCurrentIndexChanged: backend.set_active_board(currentIndex)
			Repeater {
				model: backend.boardCount
				TabButton {
					required property int index
					text: "Receive board " + (index + 1) + "  ·  " + backend.boardHosts[index]
				}
			}
		}
		StackLayout {
			Layout.fillWidth: true
			Layout.fillHeight: true
			currentIndex: backend.radarMode ? 2 : (backend.angleMode ? 1 : 0)


			// SCREEN 1: Multi-Antenna Waterfall/Chart Grid View
			ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
				spacing: 8
				visible: !backend.angleMode && !backend.radarMode

		Repeater {
			model: window.frontViewRows.length
			RowLayout {
				required property int index
				property var rowAntids: window.frontViewRows[index]
				Layout.fillWidth: true
				Layout.fillHeight: true
				spacing: 8

				Repeater {
					model: rowAntids.length
					Rectangle {
						id: antennaPanel
						required property int index
						property int antid: rowAntids[index]
						Layout.fillWidth: true
						Layout.fillHeight: true
						color: "#101014"
						border.color: window.antennaColor(antid)
						border.width: 2
						radius: 4

						ColumnLayout {
							anchors.fill: parent
							anchors.margins: 6
							spacing: 4

							Label {
								text: "Antenna " + antennaPanel.antid
								color: window.antennaColor(antennaPanel.antid)
								font.bold: true
								font.pixelSize: 13
							}

							Item {
								Layout.fillWidth: true
								Layout.fillHeight: true

								Image {
									id: displayImage
									anchors.fill: parent
									visible: !backend.timeDomain && !backend.constellation && !backend.spectrum && !backend.angleMode
									fillMode: Image.Stretch
									cache: false
									smooth: false
									source: "image://display" + antennaPanel.antid

									Timer {
										interval: 1000 / 25
										running: !backend.timeDomain && !backend.constellation && !backend.spectrum && !backend.angleMode
										repeat: true
										onTriggered: displayImage.source =
											"image://display" + antennaPanel.antid + "?" + Date.now()
									}
								}

								Loader {
									anchors.fill: parent
									active: backend.timeDomain
									sourceComponent: Component {
										ChartView {
											id: timeChart
											property int antid: antennaPanel.antid
											legend.visible: false
											antialiasing: true
											backgroundColor: "#101014"
											backgroundRoundness: 0
											dropShadowEnabled: false
											margins { top: 0; bottom: 0; left: 0; right: 0 }

											ValueAxis {
												id: sampleAxis
												min: 0
												max: Math.max(1, backend.traceSampleCount - 1)
												labelsVisible: false
												gridVisible: false
												lineVisible: false
											}

											ValueAxis {
												id: adcAxis
												min: -backend.adcFullScale
												max: backend.adcFullScale
												tickCount: 5
												labelsVisible: false
												lineVisible: false
												gridLineColor: "#37373b"
											}

											ValueAxis {
												id: correlationAxis
												min: -1
												max: 1
												labelsVisible: false
												lineVisible: false
												gridVisible: false
											}

											LineSeries {
												id: iTrace
												axisX: sampleAxis
												axisY: adcAxis
												color: "#2c7fb8"
												width: 1.25
												useOpenGL: Qt.platform.os === "linux"
											}

											LineSeries {
												id: qTrace
												axisX: sampleAxis
												axisY: adcAxis
												color: "#d95f0e"
												width: 1.25
												useOpenGL: Qt.platform.os === "linux"
											}

											LineSeries {
												id: correlationTrace
												axisX: sampleAxis
												axisY: correlationAxis
												color: "#f2c14e"
												width: 1.5
												visible: backend.pingMode
											}

											ScatterSeries {
												id: correlationPeaks
												axisX: sampleAxis
												axisY: correlationAxis
												color: "#ffffff"
												borderColor: "#f2c14e"
												markerSize: 8
												visible: backend.pingMode
											}

											Timer {
												interval: 1000 / 25
												running: true
												repeat: true
												onTriggered: {
													const stats = backend.updateTimeChart(timeChart.antid, iTrace, qTrace, correlationTrace, correlationPeaks)
													if (stats.rmsDbfs !== undefined) {
														timeStats.rmsDbfs = stats.rmsDbfs
														timeStats.peakDbfs = stats.peakDbfs
														timeStats.pingIndices = stats.pingIndices === undefined || stats.pingIndices === "" ? "-" : stats.pingIndices
																if (stats.correlationLimit !== undefined) {
																	correlationAxis.min = -stats.correlationLimit
																	correlationAxis.max = stats.correlationLimit
																}
													}
												}
											}
										}
									}
								}

								Loader {
									anchors.fill: parent
									active: backend.spectrum
									sourceComponent: Component {
										ChartView {
											id: spectrumChart
											property int antid: antennaPanel.antid
											legend.visible: false
											antialiasing: true
											backgroundColor: "#101014"
											backgroundRoundness: 0
											dropShadowEnabled: false
											margins { top: 0; bottom: 0; left: 0; right: 0 }

											ValueAxis {
												id: spectrumFrequencyAxis
												min: 2397
												max: 2477
												tickCount: 5
												labelFormat: "%.1f"
												labelsColor: "#9aa4b2"
												labelsFont.pixelSize: 8
												lineVisible: false
												gridLineColor: "#37373b"
												titleText: "<font color='#9aa4b2'>Frequency [MHz]</font>"
												titleFont.pixelSize: 9
											}

											ValueAxis {
												id: spectrumAmplitudeAxis
												min: -90
												max: 0
												tickCount: 4
												labelFormat: "%.0f"
												labelsColor: "#9aa4b2"
												labelsFont.pixelSize: 8
												lineVisible: false
												gridLineColor: "#37373b"
												titleText: "<font color='#9aa4b2'>Amplitude [dBFS]</font>"
												titleFont.pixelSize: 9
											}

											LineSeries {
												id: spectrumLine
												axisX: spectrumFrequencyAxis
												axisY: spectrumAmplitudeAxis
												color: window.antennaColor(spectrumChart.antid)
												width: 1.25
												useOpenGL: Qt.platform.os === "linux"
											}

											Timer {
												interval: 1000 / 25
												running: true
												repeat: true
												onTriggered: backend.updateSpectrumChart(spectrumChart.antid, spectrumLine, spectrumFrequencyAxis)
											}
										}
									}
								}

								Loader {
									anchors.centerIn: parent
									width: Math.min(parent.width, parent.height)
									height: width
									active: backend.constellation
									sourceComponent: Component {
										ChartView {
											id: constellationChart
											property int antid: antennaPanel.antid
											legend.visible: false
											antialiasing: true
											backgroundColor: "#101014"
											backgroundRoundness: 0
											dropShadowEnabled: false
											margins { top: 0; bottom: 0; left: 0; right: 0 }

											ValueAxis {
												id: constellationIAxis
												min: -backend.adcFullScale
												max: backend.adcFullScale
												tickCount: 5
												labelsVisible: false
												lineVisible: false
												gridLineColor: "#37373b"
											}

											ValueAxis {
												id: constellationQAxis
												min: -backend.adcFullScale
												max: backend.adcFullScale
												tickCount: 5
												labelsVisible: false
												lineVisible: false
												gridLineColor: "#37373b"
											}

											ScatterSeries {
												id: constellationPoints
												axisX: constellationIAxis
												axisY: constellationQAxis
												color: window.antennaColor(constellationChart.antid)
												borderColor: "transparent"
												markerSize: 3
												useOpenGL: Qt.platform.os === "linux"
											}

											Timer {
												interval: 1000 / 25
												running: true
												repeat: true
												onTriggered: backend.updateConstellationChart(constellationChart.antid, constellationPoints)
											}

											Label {
												anchors.top: parent.top
												anchors.left: parent.left
												anchors.margins: 4
												text: "Q"
												color: "#9aa4b2"
												font.pixelSize: 10
												z: 2
											}

											Label {
												anchors.right: parent.right
												anchors.bottom: parent.bottom
												anchors.margins: 4
												text: "I"
												color: "#9aa4b2"
												font.pixelSize: 10
												z: 2
											}
										}
									}
								}

								Label {
									id: timeStats
									property real rmsDbfs: NaN
									property real peakDbfs: NaN
									property string pingIndices: "-"
									anchors.top: parent.top
									anchors.right: parent.right
									anchors.margins: 6
									visible: backend.timeDomain
									z: 2
									text: backend.pingMode
										? "RMS   " + window.formatDbfs(rmsDbfs) + "\nPeak  " + window.formatDbfs(peakDbfs) + "\nPeak indices  " + pingIndices
										: "RMS   " + window.formatDbfs(rmsDbfs) + "\nPeak  " + window.formatDbfs(peakDbfs)
									color: "#d8dee9"
									font.family: "monospace"
									font.pixelSize: 11
									padding: 5
									background: Rectangle {
										color: "#c0101014"
										border.color: "#50545a"
										border.width: 1
										radius: 3
									}
								}
							}
						}
					}
				}
			}
		}
	}
		
	Loader {
        Layout.fillWidth: true
        Layout.fillHeight: true
		active: backend.angleMode
		sourceComponent: Component {
			Rectangle {
				color: "#101014"
				border.color: "#3c4654"
				border.width: 1

				ColumnLayout {
					anchors.centerIn: parent
					anchors.fill: parent
					anchors.margins: 28
					spacing: 18

					Label {
						Layout.alignment: Qt.AlignHCenter
						text: "Ping angle of arrival"
						color: "#d7dde5"
						font.pixelSize: 24
						font.bold: true
					}

					Label {
						Layout.alignment: Qt.AlignHCenter
						text: isFinite(backend.angleEstimate) ? backend.angleEstimate.toFixed(1) + " deg azimuth" : "--"
						color: "#f2c14e"
						font.pixelSize: 72
						font.bold: true
					}

					Label {
						Layout.alignment: Qt.AlignHCenter
						text: isFinite(backend.elevationEstimate) ? backend.elevationEstimate.toFixed(1) + " deg elevation" : "-- elevation"
						color: "#8ecae6"
						font.pixelSize: 24
					}

					ChartView {
						id: angleChart
						Layout.fillWidth: true
						Layout.fillHeight: true
						Layout.minimumHeight: 260
						legend.visible: false
						antialiasing: true
						backgroundColor: "#101014"
						backgroundRoundness: 0
						margins { top: 8; bottom: 8; left: 8; right: 8 }

						ValueAxis {
							id: angleAxis
							min: -90
							max: 90
							tickCount: 7
							labelFormat: "%.0f deg"
							labelsColor: "#9aa4b2"
							gridLineColor: "#37373b"
						}

						ValueAxis {
							id: anglePowerAxis
							min: 0
							max: 1
							tickCount: 5
							labelsColor: "#9aa4b2"
							gridLineColor: "#37373b"
						}

						LineSeries {
							id: angleSpectrum
							axisX: angleAxis
							axisY: anglePowerAxis
							color: "#f2c14e"
							width: 2
						}

						Timer {
							interval: 100
							running: true
							repeat: true
							onTriggered: backend.updateAngleChart(angleSpectrum)
						}
					}

					Label {
						Layout.alignment: Qt.AlignHCenter
						Layout.fillWidth: true
						text: backend.angleStatus
						color: isFinite(backend.angleEstimate) ? "#9fd18b" : "#d8a657"
						horizontalAlignment: Text.AlignHCenter
						wrapMode: Text.Wrap
					}

					Label {
						Layout.alignment: Qt.AlignHCenter
						text: "Calibrated delay-and-sum beamforming. The curve is an azimuth cut at the estimated elevation."
						color: "#9aa4b2"
						horizontalAlignment: Text.AlignHCenter
						wrapMode: Text.Wrap
					}
				}
			}
		}
	}

	/** Adapt the waterfall image width to the window; debounce resize events.
	 *  Qt Charts modes render directly at their item's resolution. **/
	onWidthChanged: displayWidthDebounce.restart()
	Component.onCompleted: displayWidthDebounce.restart()
	Timer {
		id: displayWidthDebounce
		interval: 300
		repeat: false
		onTriggered: backend.set_display_width(Math.round((window.width - 60) / 4))
	}

		Loader {
		id: radarModeLoader
        Layout.fillWidth: true
        Layout.fillHeight: true
		active: backend.radarMode

		sourceComponent: Component {
			Rectangle {
				id: radarRoot
				color: "#101014"
				anchors.fill: parent

				ColumnLayout {
					anchors.fill: parent
					anchors.margins: 20
					spacing: 12

					Label {
						Layout.alignment: Qt.AlignHCenter
						text: "2D Range-Azimuth Radar Mapping View"
						color: "#ffffff"
						font.pixelSize: 20
						font.bold: true
					}

					// Combined Radar Visual Assembly Container
					Item {
						id: radarPlotContainer
						Layout.fillWidth: true
						Layout.fillHeight: true

						// Keep the source texture at the backend data resolution.
						// ShaderEffectSource hides it from the window without fading it.
						Canvas {
							id: plotCanvas
							// Bind dimensions directly to backend properties matching the working demo structure
							width: backend.angleSize > 0 ? backend.angleSize : 256
							height: backend.delaySize > 0 ? backend.delaySize : 256

							renderTarget: Canvas.Image
							renderStrategy: Canvas.Immediate

							property var imageData: undefined

							function createImageData() {
								if (!available || width <= 0 || height <= 0) return;
                                // A deferred resize must not overwrite a newer received frame.
                                if (imageData && imageData.width === width && imageData.height === height) return;
								const ctx = plotCanvas.getContext("2d");
								if (ctx) {
									imageData = ctx.createImageData(width, height);
									// Show the heatmap floor even before the first IQ frame.
									for (let i = 0; i < imageData.data.length; i += 4) {
										imageData.data[i] = 68;
										imageData.data[i + 1] = 1;
										imageData.data[i + 2] = 84;
										imageData.data[i + 3] = 255;
									}
									requestPaint();
								}
							}

							onAvailableChanged: if (available) createImageData();
							onWidthChanged: if (available) createImageData();
							onHeightChanged: if (available) createImageData();

							onPaint: {
								if (available && plotCanvas.imageData) {
									const ctx = plotCanvas.getContext("2d");
									if (ctx) {
										ctx.drawImage(plotCanvas.imageData, 0, 0);
									}
								}
							}
						}

						// Main Visual Panel (Fills container to properly share the bottom origin baseline)
						Rectangle {
							id: plotArea
							anchors.fill: parent
							color: "transparent" // Let background handle layout coloration

							property color lineColor: Qt.rgba(0.3, 0.3, 0.3, 0.9)
							property real lineWidth: 2.5

							ShaderEffect {
								id: shader
								// Match the grid radius and its bottom-center origin.
								width: 2 * gridOverlayCanvas.plotRadius
								height: gridOverlayCanvas.plotRadius
								anchors.horizontalCenter: parent.horizontalCenter
								anchors.bottom: parent.bottom
                                anchors.bottomMargin: 28
								onStatusChanged: if (status === ShaderEffect.Error) console.warn("Radar shader:", log)

								property variant source: ShaderEffectSource {
									sourceItem: plotCanvas
									hideSource: true
									smooth: true
									live: backend.radarMode
								}

								property int delay_min: backend.delayMin
								property int delay_max: backend.delayMax

								vertexShader: "vertex_shader.qsb"
								fragmentShader: "fragment_shader.qsb"
							}
						}

						// Polar Vector Ring Overlay Grid (Layered correctly on top)
						Canvas {
							id: gridOverlayCanvas
                            objectName: "radarGridOverlay"
							anchors.fill: parent
							z: 10

							property real plotRadius: Math.max(0, Math.min((width - 180) / 2, height - 68))
							property var origin_x: 0.5 * gridOverlayCanvas.width
							property var origin_y: gridOverlayCanvas.height - 28
							property var phi_num: 18
							property var text_color: "white"
							property var text_width: 1
							property var text_font: "14px sans-serif"

							property var delay_min: backend.delayMin
							property var delay_max: backend.delayMax

							onPaint: {
								var context = getContext("2d");
								if (!context) return;

								context.clearRect(0, 0, gridOverlayCanvas.width, gridOverlayCanvas.height);
								context.font = text_font;
								context.fillStyle = text_color;
								context.lineWidth = text_width;

								const radius = gridOverlayCanvas.plotRadius;
                                const totalSamples = delay_max - delay_min;
                                const metersPerSample = backend.radarMetersPerSample;
                                const zero = backend.radarZeroSample;
                                if (radius <= 0) return;
                                const rangeStart = (delay_min - zero) * metersPerSample;
                                const rangeEnd = (delay_max - zero) * metersPerSample;
                                const span = totalSamples * metersPerSample;
                                // Rounded meter intervals adapt to zoom and available label space.
                                const targetStep = span / Math.max(2, Math.floor(radius / 75));
                                const magnitude = Math.pow(10, Math.floor(Math.log10(targetStep)));
                                const normalized = targetStep / magnitude;
                                const majorStep = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude;
                                const decimals = Math.max(1, Math.ceil(-Math.log10(majorStep)));
                                function label(text, x, y, color, baseline) {
                                    context.save();
                                    context.font = "12px monospace";
                                    context.textAlign = "center";
                                    context.textBaseline = baseline || "middle";
                                    context.lineWidth = 3;
                                    context.strokeStyle = "#101014";
                                    context.strokeText(text, x, y);
                                    context.fillStyle = color;
                                    context.fillText(text, x, y);
                                    context.restore();
                                }
                                function ring(fraction, major) {
                                    context.beginPath();
                                    context.lineWidth = major ? 1.3 : 0.65;
                                    context.strokeStyle = major ? "rgba(225,235,245,0.60)" : "rgba(225,235,245,0.22)";
                                    context.arc(origin_x, origin_y, fraction * radius, Math.PI, 2 * Math.PI);
                                    context.stroke();
                                }
                                if (isFinite(zero)) {
                                    const minorStep = majorStep / 5;
                                    for (let tick = Math.ceil(rangeStart / minorStep); tick <= Math.floor(rangeEnd / minorStep); ++tick) {
                                        const distance = tick * minorStep;
                                        const fraction = (distance - rangeStart) / span;
                                        if (fraction <= 0 || fraction >= 1) continue;
                                        const major = tick % 5 === 0;
                                        ring(fraction, major);
                                        if (major) {
                                            const r = fraction * radius;
                                            const text = distance.toFixed(decimals) + " m";
                                            if (r > 65) label(text, origin_x + r * 0.707, origin_y - r * 0.707, "#bce7ff");
                                            if (r > 70 && radius - r > 70) {
                                                label(text, origin_x + r, origin_y + 8, "#bce7ff", "top");
                                                label(text, origin_x - r, origin_y + 8, "#bce7ff", "top");
                                            }
                                        }
                                    }
                                    label(rangeStart.toFixed(decimals) + " m", origin_x, origin_y + 8, "#bce7ff", "top");
                                    for (const side of [-1, 1])
                                        label(rangeEnd.toFixed(decimals) + " m", origin_x + side * radius, origin_y + 8, "#bce7ff", "top");
                                } else {
                                    for (let i = 1; i < 20; ++i) ring(i / 20, i % 5 === 0);
                                }
                                ring(1, true);

								// 2. Draw Angular/Azimuth Spoke Lines
								for (let i = -phi_num / 2; i <= phi_num / 2; i++) {
									const radians = (i / phi_num) * Math.PI;
									context.beginPath();
									const majorAngle = i % 3 === 0;
                                    context.strokeStyle = majorAngle ? "rgba(225,235,245,0.55)" : "rgba(225,235,245,0.22)";
                                    context.lineWidth = majorAngle ? 1.2 : 0.65;
									context.moveTo(origin_x, origin_y);
									context.lineTo(origin_x + Math.sin(radians) * radius, origin_y - Math.cos(radians) * radius);
									context.stroke();

									if (i % 3 !== 0) continue;
                                    let angleText = (180 / phi_num * (-i)) + "°";
									context.fillStyle = text_color;

									if (i < 0) {
										context.textAlign = "right";
										context.textBaseline = "bottom";
									} else if (i == 0) {
										context.textAlign = "center";
										context.textBaseline = "bottom";
									} else {
										context.textAlign = "left";
										context.textBaseline = "bottom";
									}
									context.strokeStyle = "#101014";
                                    context.lineWidth = 3;
                                    context.strokeText(angleText, origin_x + Math.sin(radians) * (radius + 8), origin_y - Math.cos(radians) * (radius + 8));
                                    context.fillText(angleText, origin_x + Math.sin(radians) * (radius + 8), origin_y - Math.cos(radians) * (radius + 8));
								}
							}

							onWidthChanged: gridOverlayCanvas.requestPaint()
							onHeightChanged: gridOverlayCanvas.requestPaint()
						}

						ColumnLayout {
							id: radarColorbar
							anchors.right: parent.right
							anchors.rightMargin: 18
							anchors.verticalCenter: parent.verticalCenter
							width: 72
							height: Math.max(1, Math.min(parent.height - 24, gridOverlayCanvas.plotRadius * 1.15))
							spacing: 6
							z: 11

							Label {
								Layout.alignment: Qt.AlignHCenter
								text: backend.radarLinearScale ? "Received power\n(full scale²)" : "Received power\n(dBFS)"
								color: "#d7dde5"
								font.pixelSize: 10
								horizontalAlignment: Text.AlignHCenter
								wrapMode: Text.Wrap
							}

							Item {
								Layout.fillWidth: true
								Layout.fillHeight: true

								Rectangle {
									id: radarColorbarGradient
									width: 18
									anchors.top: parent.top
									anchors.bottom: parent.bottom
									anchors.horizontalCenter: parent.horizontalCenter
									border.color: "#aab4be"
									border.width: 1
									gradient: Gradient {
										GradientStop { position: 0.0; color: "#fde725" }
										GradientStop { position: 0.25; color: "#5ec962" }
										GradientStop { position: 0.5; color: "#21918c" }
										GradientStop { position: 0.75; color: "#3b528b" }
										GradientStop { position: 1.0; color: "#440154" }
									}
								}

								Label {
									anchors.top: parent.top
									anchors.left: radarColorbarGradient.right
									anchors.leftMargin: 5
									text: { backend.radarLinearScale; backend.radarMinDb; backend.radarMaxDb; return backend.radarColorbarLabel(1); }
									color: "#ffffff"
									font.pixelSize: 10
								}
								Label {
									anchors.bottom: parent.bottom
									anchors.left: radarColorbarGradient.right
									anchors.leftMargin: 5
									text: { backend.radarLinearScale; backend.radarMinDb; backend.radarMaxDb; return backend.radarColorbarLabel(0); }
									color: "#ffffff"
									font.pixelSize: 10
								}

								Repeater {
									model: 3
									Label {
										required property int index
										anchors.left: radarColorbarGradient.right
										anchors.leftMargin: 5
										y: parent.height * (index + 1) / 4 - height / 2
										text: { backend.radarLinearScale; backend.radarMinDb; backend.radarMaxDb; return backend.radarColorbarLabel(1 - (index + 1) / 4); }
										color: "#ffffff"
										font.pixelSize: 10
									}
								}
							}
						}

					}

					// Dynamic Processing Status Label
					Label {
						id: radarStatusLabel
						Layout.alignment: Qt.AlignHCenter
						Layout.fillWidth: true
						text: backend.radarStatus + (isFinite(backend.radarZeroSample) ? " · zero: " + backend.radarZeroSample.toFixed(2) + " samples" : " · Use Sync + Calibration for range in meters")
						color: backend.txActive ? "#9fd18b" : "#d8a657"
						font.pixelSize: 15
						font.bold: true
						horizontalAlignment: Text.AlignHCenter
						wrapMode: Text.Wrap
					}
				}

				Connections {
					target: backend
					ignoreUnknownSignals: true

					function onTxStateChanged() { gridOverlayCanvas.requestPaint(); }
                    function onRadarConfigChanged() {
						gridOverlayCanvas.requestPaint();
						Qt.callLater(function() { plotCanvas.createImageData(); });
					}

					function onRadarMapChanged(image_data) {
						if (!image_data || image_data.length === 0) return;

						// The backend emits exactly w*h RGBA values. Use the emitted
						// dimensions, not a stale Canvas binding during a resize.
						let w = Math.max(1, backend.angleSize);
						let h = Math.max(1, backend.delaySize);
						if (w <= 0 || h <= 0) return;

						if (image_data.length !== w * h * 4) return;
						if (plotCanvas.width !== w || plotCanvas.height !== h) {
							plotCanvas.width = w;
							plotCanvas.height = h;
						}
						if (plotCanvas.imageData === undefined || plotCanvas.imageData.data.length !== image_data.length) {
							const ctx = plotCanvas.getContext("2d");
							if (ctx && plotCanvas.available) {
								plotCanvas.imageData = ctx.createImageData(w, h);
							}
						}

						if (plotCanvas.imageData) {
							let len = plotCanvas.imageData.data.length;
							let copyLen = Math.min(len, image_data.length);
							for (let i = 0; i < copyLen; i++) {
								plotCanvas.imageData.data[i] = image_data[i];
							}
							plotCanvas.requestPaint();
						}
					}
				}
			}
		}
	}

	}
}

}

