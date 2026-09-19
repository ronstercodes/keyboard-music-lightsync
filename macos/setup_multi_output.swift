// Creates a Multi-Output Device ("Speakers + BlackHole") that plays to your
// current output device AND BlackHole 2ch, then makes it the default output.
// Run:  swift setup_multi_output.swift            (create + switch)
//       swift setup_multi_output.swift --use "Name"  (plain default-output switch)
//       swift setup_multi_output.swift --revert   (switch back to real speakers)
import CoreAudio
import Foundation

let kName = "Speakers + BlackHole"
let kState = NSString(string: "~/.keyboard-music-lightsync-output").expandingTildeInPath   // UID of the wrapped speakers

func prop(_ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func string(_ id: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var addr = prop(sel)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var value: Unmanaged<CFString>? = nil
    let st = withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0)
    }
    guard st == noErr, let v = value else { return nil }
    return v.takeRetainedValue() as String
}

func allDevices() -> [AudioObjectID] {
    var addr = prop(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    return ids
}

func outputChannels(_ id: AudioObjectID) -> Int {
    var addr = prop(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeOutput)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let buf = UnsafeMutablePointer<AudioBufferList>.allocate(capacity: Int(size))
    defer { buf.deallocate() }
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, buf) == noErr else { return 0 }
    return UnsafeMutableAudioBufferListPointer(buf).reduce(0) { $0 + Int($1.mNumberChannels) }
}

func defaultOutput() -> AudioObjectID {
    var addr = prop(kAudioHardwarePropertyDefaultOutputDevice)
    var id: AudioObjectID = 0
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id)
    return id
}

func setDefaultOutput(_ id: AudioObjectID) {
    var dev = id
    let size = UInt32(MemoryLayout<AudioObjectID>.size)
    for sel in [kAudioHardwarePropertyDefaultOutputDevice, kAudioHardwarePropertyDefaultSystemOutputDevice] {
        var addr = prop(sel)
        let st = AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, size, &dev)
        if st != noErr { print("warning: set default (\(sel)) failed: \(st)") }
    }
}

let devices = allDevices()
func find(named n: String) -> AudioObjectID? { devices.first { string($0, kAudioObjectPropertyName) == n } }
func find(uid u: String) -> AudioObjectID? { devices.first { string($0, kAudioDevicePropertyDeviceUID) == u } }
func isVirtual(_ d: AudioObjectID) -> Bool {
    let n = string(d, kAudioObjectPropertyName) ?? ""
    return n.contains("BlackHole") || n == kName || n.contains("Teams") || n.contains("Aggregate")
}
func firstRealOutput() -> AudioObjectID? {
    // prefer external/wired outputs over the built-in speakers, never a virtual device
    let real = devices.filter { outputChannels($0) > 0 && !isVirtual($0) }
    return real.first { !(string($0, kAudioObjectPropertyName) ?? "").contains("MacBook") } ?? real.first
}

let args = CommandLine.arguments
if let i = args.firstIndex(of: "--use"), i + 1 < args.count {
    // plain "make this device the default output" (no aggregate involved)
    guard let d = find(named: args[i + 1]) else { print("device not found: \(args[i + 1])"); exit(1) }
    setDefaultOutput(d); print("Default output -> \(args[i + 1])"); exit(0)
}
if args.contains("--list") {
    for d in devices where outputChannels(d) > 0 { print(string(d, kAudioObjectPropertyName) ?? "?") }
    exit(0)
}
if args.contains("--revert") {
    guard find(named: kName) != nil else { print("nothing to revert ('\(kName)' not present)"); exit(0) }
    var real: AudioObjectID? = nil
    if let uid = try? String(contentsOfFile: kState, encoding: .utf8), let d = find(uid: uid.trimmingCharacters(in: .whitespacesAndNewlines)) {
        real = d
    }
    if real == nil, let agg = find(named: kName) {
        // the speakers we wrapped are the aggregate's main sub-device
        var addr = prop(kAudioAggregateDevicePropertyMainSubDevice)
        var size = UInt32(MemoryLayout<CFString?>.size)
        var value: Unmanaged<CFString>? = nil
        let st = withUnsafeMutablePointer(to: &value) { AudioObjectGetPropertyData(agg, &addr, 0, nil, &size, $0) }
        if st == noErr, let v = value { real = find(uid: v.takeRetainedValue() as String) }
        // if the wrapped speakers disappeared, CoreAudio re-points the main sub-device at BlackHole
        if let r = real, isVirtual(r) { real = nil }
    }
    if real == nil { real = firstRealOutput() }
    if let r = real { setDefaultOutput(r); print("Default output -> \(string(r, kAudioObjectPropertyName)!)") }
    if let agg = find(named: kName) {
        usleep(200_000)
        let st = AudioHardwareDestroyAggregateDevice(agg)
        print(st == noErr ? "Removed '\(kName)'" : "destroy failed: \(st)")
    }
    exit(0)
}

guard let bh = find(named: "BlackHole 2ch"), let bhUID = string(bh, kAudioDevicePropertyDeviceUID) else {
    print("BlackHole 2ch not found. Install it first: brew install --cask blackhole-2ch"); exit(1)
}

var master = defaultOutput()
if let existing = find(named: kName), master == existing {
    // already using our aggregate; find the real speakers inside it instead
    print("'\(kName)' is already the default output."); exit(0)
}
if isVirtual(master) {
    // default output is BlackHole / a stale aggregate: wrap a real device instead
    guard let r = firstRealOutput() else { print("no physical output device found"); exit(1) }
    print("warning: default output was \(string(master, kAudioObjectPropertyName) ?? "virtual"); using \(string(r, kAudioObjectPropertyName) ?? "?") instead")
    master = r
}
guard let masterUID = string(master, kAudioDevicePropertyDeviceUID) else { print("no default output"); exit(1) }
let masterName = string(master, kAudioObjectPropertyName) ?? masterUID

if let old = find(named: kName) { _ = AudioHardwareDestroyAggregateDevice(old) }

let desc: [String: Any] = [
    kAudioAggregateDeviceNameKey: kName,
    kAudioAggregateDeviceUIDKey: "com.keyboard-music-lightsync.multiout",
    kAudioAggregateDeviceIsStackedKey: 1,          // 1 = Multi-Output Device
    // BlackHole is the clock source (stable, virtual); the speakers are drift-corrected.
    // With the speakers as master, a Bluetooth output starves BlackHole of audio.
    kAudioAggregateDeviceMainSubDeviceKey: bhUID,
    kAudioAggregateDeviceSubDeviceListKey: [
        [kAudioSubDeviceUIDKey: bhUID],
        [kAudioSubDeviceUIDKey: masterUID, kAudioSubDeviceDriftCompensationKey: 1],
    ],
]
try? masterUID.write(toFile: kState, atomically: true, encoding: .utf8)
// A stacked device silently starves sub-devices whose nominal rate differs from
// the main one (seen with a 44.1 kHz Bluetooth soundbar + 48 kHz BlackHole), so
// pin BlackHole to the speakers' rate first.
func nominalRate(_ id: AudioObjectID) -> Double {
    var addr = prop(kAudioDevicePropertyNominalSampleRate)
    var rate: Double = 0; var size = UInt32(MemoryLayout<Double>.size)
    AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &rate); return rate
}
func setNominalRate(_ id: AudioObjectID, _ rate: Double) -> Bool {
    var addr = prop(kAudioDevicePropertyNominalSampleRate)
    var r = rate
    return AudioObjectSetPropertyData(id, &addr, 0, nil, UInt32(MemoryLayout<Double>.size), &r) == noErr
}
let masterRate = nominalRate(master)
if masterRate > 0 && nominalRate(bh) != masterRate {
    if setNominalRate(bh, masterRate) { usleep(200_000); print("BlackHole sample rate -> \(Int(masterRate)) Hz to match \(masterName)") }
    else { print("warning: could not set BlackHole to \(Int(masterRate)) Hz") }
}

var aggID: AudioObjectID = 0
let st = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &aggID)
guard st == noErr else { print("create failed: \(st)"); exit(1) }
usleep(300_000)
setDefaultOutput(aggID)
print("Created '\(kName)' = \(masterName) + BlackHole 2ch, and set it as default output.")
print("Note: the volume keys don't control a Multi-Output Device; set volume on \(masterName) in Audio MIDI Setup, or run with --revert to go back.")
