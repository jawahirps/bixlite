import SwiftUI

enum Brand {
    static let navy = Color(red: 0x0b / 255, green: 0x22 / 255, blue: 0x33 / 255)
    static let navy2 = Color(red: 0x0b / 255, green: 0x2c / 255, blue: 0x3c / 255)
    static let teal = Color(red: 0x0f / 255, green: 0x7c / 255, blue: 0x97 / 255)

    static let gradient = LinearGradient(colors: [navy, navy2, teal],
                                         startPoint: .topLeading, endPoint: .bottomTrailing)
}

struct BigButtonStyle: ButtonStyle {
    var filled: Bool = true
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .frame(maxWidth: .infinity, minHeight: 58)
            .foregroundStyle(filled ? Brand.navy : .white)
            .background(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .fill(filled ? Color.white : Color.white.opacity(0.12))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .stroke(Color.white.opacity(filled ? 0 : 0.5), lineWidth: 1.5)
            )
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}
