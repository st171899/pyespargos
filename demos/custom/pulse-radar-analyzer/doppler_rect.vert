#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 uv;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float xLow;
    float xHigh;
};
void main() {
    gl_Position = qt_Matrix * qt_Vertex;
    uv = vec2(mix(xLow, xHigh, qt_MultiTexCoord0.x), 1.0-qt_MultiTexCoord0.y);
}
