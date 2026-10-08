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
	minimumWidth: 1280
	minimumHeight: 800

	title: "IQ Signal Analyzer"

	// Universal antenna colour coding (same palette/order as the web interface)
	property var colorCycle: ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]
	property var antidToColorIndex: [4, 5, 6, 7, 0, 1, 2, 3]
	function antennaColor(antid) { return colorCycle[antidToColorIndex[antid]] }
	function formatDbfs(value) { return isFinite(value) ? value.toFixed(1) + " dBFS" : "−∞ dBFS" }

	// Front view of the array: top row antids 4..7, bottom row 0..3
	property var frontViewRows: [[4, 5, 6, 7], [0, 1, 2, 3]]

	/** RX drawer: the reusable IQ capture settings (replaces the CSI pool drawer) **/
	receiverDrawerComponent: Component {
		IQ.IQRxDrawer {
			controller: iqcontrol
		}
	}

	/** App drawer: app-specific display settings only **/
	appDrawerComponent: Component {
		Common.AppDrawer {
			id: appDrawer
			title: "Display"
			endpoint: appconfig
			// Keep this addon's grid narrower than the drawer viewport. AppDrawer
			// then has no horizontal overflow to scroll, without changing the
			// shared component used by WiFi demos.
			contentLayout.width: Math.max(0, appDrawer.width - 40)

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

			Label { text: "Remove line of sight"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			Switch {
				property string configKey: "remove_los"
				property string configProp: "checked"
				Component.onCompleted: appDrawer.configManager.register(this)
				onCheckedChanged: appDrawer.configManager.onControlChanged(this)
				checked: false
				ToolTip.visible: hovered
				ToolTip.text: "Subtract the strongest coherent direct-path chirp from angle estimation and Ping TX / Time IQ only."
			}

			Label { text: "Chirp length [frames]"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: chirpFramesControl
				property string configKey: "chirp_frames"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 4
				to: 256
				value: 16
				stepSize: 1
				editable: true
				ToolTip.visible: hovered
				ToolTip.text: "Number of 40 MHz samples in each chirp."
			}

			Label { text: "Bandwidth [Hz]"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: bandwidthHzControl
				property string configKey: "bandwidth_hz"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 1000000
				to: 40000000
				value: 40000000
				stepSize: 1000000
				editable: true
				ToolTip.visible: hovered
				ToolTip.text: "LFM chirp sweep bandwidth from -bandwidth/2 to +bandwidth/2."
			}

			Label { text: "TX chirps / 256"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: pingsPer256Control
				property string configKey: "pings_per_256"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: 1
				to: 32
				value: 2
				stepSize: 1
				editable: true
				ToolTip.visible: hovered
				ToolTip.text: "Number of chirp bursts placed in the 256-sample TX waveform."
			}

			Label { text: "TX gain [dB]"; color: "#ffffff"; Layout.alignment: Qt.AlignRight; Layout.fillWidth: true }
			SpinBox {
				id: txGainControl
				property string configKey: "tx_gain_db"
				property string configProp: "value"
				Component.onCompleted: appDrawer.configManager.register(this)
				onValueChanged: appDrawer.configManager.onControlChanged(this)
				from: -90
				to: 0
				value: -20
				stepSize: 1
				editable: true
				ToolTip.visible: hovered
				ToolTip.text: "Pluto TX hardware gain, from -90 dB to 0 dB. The hardware applies a minimum of about -89.75 dB."
			}

			Button {
				text: "Start TX"
				onClicked: backend._start_ping_tx(
					txGainControl.value,
					chirpFramesControl.value,
					bandwidthHzControl.value,
					pingsPer256Control.value
				)
			}

			Button {
				text: "Stop TX"
				onClicked: backend._stop_ping_tx()
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
			visible: backend.boardCount > 1
			Layout.fillWidth: true
			onCurrentIndexChanged: backend.set_active_board(currentIndex)
			Repeater {
				model: backend.boardCount
				TabButton {
					required property int index
					text: "Array " + (index + 1) + "  ·  " + backend.boardHosts[index]
				}
			}
		}

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
		anchors.fill: parent
		active: backend.angleMode
		z: 10
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
						text: "Pluto ping angle of arrival"
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
						color: backend.txActive ? "#9fd18b" : "#d8a657"
						horizontalAlignment: Text.AlignHCenter
						wrapMode: Text.Wrap
					}

					Label {
						Layout.alignment: Qt.AlignHCenter
						text: "Run the existing IQ calibration first. The calibrated relative phases and configured 2D antenna geometry drive the spatial FFT."
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
		anchors.fill: parent
		active: backend.radarMode
		z: 11

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

						// SIBLING TEXTURE GENERATOR: Visible to Scene Graph but transparent to bypass rendering optimization cuts
						Canvas {
							id: plotCanvas
							// Bind dimensions directly to backend properties matching the working demo structure
							width: backend.angleSize > 0 ? backend.angleSize : 256
							height: backend.delaySize > 0 ? backend.delaySize : 256
							
							visible: true
							opacity: 0.01 // Retains lifecycle rendering passes in GPU memory maps
							anchors.fill: parent

							renderTarget: Canvas.FramebufferObject
							renderStrategy: Canvas.Threaded

							property var imageData: undefined
							
							function createImageData() {
								if (!available || width <= 0 || height <= 0) return;
								const ctx = plotCanvas.getContext("2d");
								if (ctx) {
									imageData = ctx.createImageData(width, height);
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
								anchors.fill: parent
								visible: status === ShaderEffect.Ready

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
							anchors.fill: parent
							z: 10 
							
							property var origin_x: 0.5 * gridOverlayCanvas.width
							property var origin_y: gridOverlayCanvas.height // Match bottom edge baseline exactly
							property var phi_num: 6
							property var text_color: "white"
							property var text_width: 1
							property var text_font: "14px sans-serif"
							
							property var delay_min: backend.delayMin
							property var delay_max: backend.delayMax
							property var num_delay: 4

							onPaint: {
								var context = getContext("2d");
								if (!context) return;
								
								context.clearRect(0, 0, gridOverlayCanvas.width, gridOverlayCanvas.height);
								context.font = text_font;
								context.fillStyle = text_color;
								context.lineWidth = text_width;
								context.textAlign = "right";
								context.textBaseline = "top";
								
								const radius = Math.min(gridOverlayCanvas.width / 2, gridOverlayCanvas.height - 40);
								
								for (let i = 0; i < num_delay; i++) {
									context.beginPath();
									context.strokeStyle = "rgba(220, 230, 240, 0.35)";
									context.lineWidth = 1.5;
									context.arc(origin_x, origin_y, (1 - (1 / num_delay) * i) * radius, Math.PI, 2 * Math.PI);
									context.stroke();
								}
								
								for (let i = -phi_num / 2; i <= phi_num / 2; i++) {
									const radians = (i / phi_num) * Math.PI;
									context.beginPath();
									context.strokeStyle = "rgba(220, 230, 240, 0.35)";
									context.lineWidth = 1.0;
									context.moveTo(origin_x, origin_y);
									context.lineTo(origin_x + Math.sin(radians) * radius, origin_y - Math.cos(radians) * radius);
									context.stroke();
									
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
									context.fillText(angleText, origin_x + Math.sin(radians) * (radius + 5), origin_y - Math.cos(radians) * (radius + 5));
								}
							}

							onWidthChanged: gridOverlayCanvas.requestPaint()
							onHeightChanged: gridOverlayCanvas.requestPaint()
						}
					}

					// Dynamic Processing Status Label
					Label {
						id: radarStatusLabel
						Layout.alignment: Qt.AlignHCenter
						Layout.fillWidth: true
						text: backend.radarStatus
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
					
					function onRadarConfigChanged() {
						gridOverlayCanvas.requestPaint();
						plotCanvas.createImageData();
					}

					function onRadarMapChanged(image_data) {
						if (!image_data || image_data.length === 0) return;

						let w = backend.angleSize;
						let h = backend.delaySize;
						if (w <= 0 || h <= 0) return;

						if (plotCanvas.imageData === undefined || image_data.length !== plotCanvas.imageData.data.length) {
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
