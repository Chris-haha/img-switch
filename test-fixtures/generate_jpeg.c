/* Generate the independent JPEG fixtures with macOS ImageIO, not the validator.
 * clang generate_jpeg.c -framework CoreGraphics -framework ImageIO \
 *   -framework CoreFoundation -o /tmp/img-switch-jpeg-fixtures
 * /tmp/img-switch-jpeg-fixtures test-fixtures
 */
#include <CoreGraphics/CoreGraphics.h>
#include <ImageIO/ImageIO.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc != 2) return 1;
    for (int gray = 0; gray < 2; gray++) {
        size_t width = gray ? 17 : 37, height = gray ? 19 : 29;
        size_t channels = gray ? 1 : 3, length = width * height * channels;
        unsigned char *pixels = malloc(length);
        if (!pixels) return 2;
        for (size_t y = 0; y < height; y++)
            for (size_t x = 0; x < width; x++)
                for (size_t c = 0; c < channels; c++)
                    pixels[(y * width + x) * channels + c] = (x * 17 + y * 23 + c * 71 + x * y * 3) % 256;
        CFDataRef data = CFDataCreate(NULL, pixels, length);
        CGDataProviderRef provider = CGDataProviderCreateWithCFData(data);
        CGColorSpaceRef color = gray ? CGColorSpaceCreateDeviceGray() : CGColorSpaceCreateDeviceRGB();
        CGImageRef image = CGImageCreate(width, height, 8, 8 * channels, width * channels,
                                        color, kCGImageAlphaNone, provider, NULL, false,
                                        kCGRenderingIntentDefault);
        for (int progressive = 0; progressive < 2; progressive++) {
            char path[4096];
            snprintf(path, sizeof(path), "%s/imageio-%s-%s.jpg", argv[1], gray ? "gray" : "color",
                     progressive ? "progressive" : "baseline");
            CFURLRef url = CFURLCreateFromFileSystemRepresentation(NULL, (UInt8 *)path, strlen(path), false);
            CGImageDestinationRef dest = CGImageDestinationCreateWithURL(url, CFSTR("public.jpeg"), 1, NULL);
            CFMutableDictionaryRef options = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                                                         &kCFTypeDictionaryValueCallBacks);
            CFMutableDictionaryRef jfif = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                                                      &kCFTypeDictionaryValueCallBacks);
            CFDictionarySetValue(jfif, kCGImagePropertyJFIFIsProgressive,
                                 progressive ? kCFBooleanTrue : kCFBooleanFalse);
            CFDictionarySetValue(options, kCGImagePropertyJFIFDictionary, jfif);
            CGImageDestinationAddImage(dest, image, options);
            if (!CGImageDestinationFinalize(dest)) return 3;
            CFRelease(jfif); CFRelease(options); CFRelease(dest); CFRelease(url);
        }
        CGImageRelease(image); CGColorSpaceRelease(color); CGDataProviderRelease(provider);
        CFRelease(data); free(pixels);
    }
    return 0;
}
