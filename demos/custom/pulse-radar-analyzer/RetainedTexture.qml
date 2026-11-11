import QtQuick

// Keep a ready texture separate from the image currently being requested.
// Some render backends briefly clear an Image texture during replacement even
// with retainWhileLoading; the shader must always sample the ready front item.
Item {
    id: root
    property url source
    property int front: 0
    property bool initialized: false

    function requestFrame() {
        if (!initialized) return;
        const target = front === 0 ? second : first;
        target.source = source;
        if (target.status === Image.Ready) acceptFrame(target, front === 0 ? 1 : 0);
    }
    function acceptFrame(item, index) {
        if (item.status === Image.Ready && item.source.toString() === source.toString())
            front = index;
    }
    onSourceChanged: requestFrame()
    Component.onCompleted: { initialized = true; requestFrame(); }

    Image {
        id: first
        anchors.fill: parent
        visible: root.front === 0
        cache: false
        asynchronous: true
        onStatusChanged: root.acceptFrame(first, 0)
    }
    Image {
        id: second
        anchors.fill: parent
        visible: root.front === 1
        cache: false
        asynchronous: true
        onStatusChanged: root.acceptFrame(second, 1)
    }
}
