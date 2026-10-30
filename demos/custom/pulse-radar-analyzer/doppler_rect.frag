#version 440
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 fragColor;
layout(binding = 1) uniform sampler2D source;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float xLow;
    float xHigh;
};
void main() {
    vec2 size = vec2(textureSize(source, 0));
    if (uv.x < 0.0 || uv.x > 1.0) { fragColor = vec4(0.067, 0.063, 0.078, 1.0) * qt_Opacity; return; }
    vec2 sampleUV = (uv * (size - 1.0) + 0.5) / size;
    fragColor = texture(source, sampleUV) * qt_Opacity;
}
